"""Instants and the cut-off (design section 4.3).

Every comparison with a cut-off is made on instants. The store keeps one canonical UTC text,
still reads the ISO form with an offset that earlier versions wrote, and refuses a value
without an offset. Naive datetimes are refused wherever they enter.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from openreserve.core.exact import NaiveDatetimeError
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import (
    Account,
    Currency,
    IntegrityError,
    Money,
    OwnerType,
    TransactionStatus,
)

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
JST = timezone(timedelta(hours=9))
EST = timezone(timedelta(hours=-5))
REPO = Path(__file__).parent.parent


def _ledger() -> tuple[Ledger, SQLiteLedgerStorage]:
    storage = SQLiteLedgerStorage(":memory:")
    return Ledger(storage), storage


def _open(storage: SQLiteLedgerStorage, account_id: str, owner_type: OwnerType, created_at: datetime = T) -> str:
    storage.save_account(
        Account(account_id=account_id, owner_type=owner_type, currency=Currency.JPY, label=account_id, created_at=created_at)
    )
    return account_id


def _transfer(ledger, src, dst, amount, *, initiated_at=T, settled_at=T) -> str:
    b = TransactionBuilder("MOVE", "test", initiated_at=initiated_at)
    b.transfer(src, dst, Money(cents=amount, currency=Currency.JPY))
    ledger.post(b.build())
    if settled_at is not None:
        ledger.settle(b.transaction_id, settled_at=settled_at)
    return b.transaction_id


@pytest.fixture
def funded():
    ledger, storage = _ledger()
    _open(storage, "platform", OwnerType.PLATFORM)
    _open(storage, "holder", OwnerType.USER)
    tx = _transfer(ledger, "platform", "holder", 100)
    yield ledger, storage, tx
    storage.close()


# ---------- the cut-off is an instant ----------


def test_cutoff_in_another_offset_gives_the_balance_of_that_instant(funded):
    ledger, _storage, _tx = funded
    # settled 2026-01-01T00:00Z
    assert ledger.balance("holder", as_of=datetime(2026, 1, 1, 8, 0, tzinfo=JST)).cents == 0  # 2025-12-31T23:00Z
    assert ledger.balance("holder", as_of=datetime(2025, 12, 31, 20, 0, tzinfo=EST)).cents == 100  # 2026-01-01T01:00Z
    assert ledger.balance("holder", as_of=datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)).cents == 0
    assert ledger.balance("holder", as_of=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)).cents == 100
    # the settlement instant itself, written three ways
    assert ledger.balance("holder", as_of=T).cents == 100
    assert ledger.balance("holder", as_of=datetime(2026, 1, 1, 9, 0, tzinfo=JST)).cents == 100
    assert ledger.balance("holder", as_of=datetime(2025, 12, 31, 19, 0, tzinfo=EST)).cents == 100


def test_cutoff_is_inclusive_to_the_microsecond(funded):
    ledger, _storage, _tx = funded
    assert ledger.balance("holder", as_of=T).cents == 100
    assert ledger.balance("holder", as_of=T - timedelta(microseconds=1)).cents == 0
    assert ledger.balance("holder", as_of=T + timedelta(microseconds=1)).cents == 100
    assert ledger.balance("platform", as_of=T).cents == -100
    assert ledger.balance("platform", as_of=T - timedelta(microseconds=1)).cents == 0


def test_settlement_given_with_an_offset_is_stored_as_canonical_utc_text():
    ledger, storage = _ledger()
    _open(storage, "platform", OwnerType.PLATFORM, created_at=datetime(2026, 1, 1, 9, 0, tzinfo=JST))
    _open(storage, "holder", OwnerType.USER)
    nine_jst = datetime(2026, 1, 1, 9, 0, tzinfo=JST)  # the same instant as T
    tx = _transfer(ledger, "platform", "holder", 100, initiated_at=nine_jst, settled_at=nine_jst)

    assert ledger.balance("holder", as_of=T).cents == 100
    row = storage._conn.execute(
        "SELECT initiated_at, settled_at FROM transactions WHERE transaction_id = ?", (tx,)
    ).fetchone()
    assert row == ("2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z")
    created = storage._conn.execute("SELECT created_at FROM accounts WHERE account_id = 'platform'").fetchone()
    assert created == ("2026-01-01T00:00:00.000000Z",)
    loaded = ledger.get_transaction(tx)
    assert loaded.settled_at == T and loaded.settled_at.utcoffset() == timedelta(0)
    assert loaded.initiated_at == T
    storage.close()


def test_a_transaction_posted_as_settled_is_stored_as_canonical_utc_text():
    ledger, storage = _ledger()
    _open(storage, "platform", OwnerType.PLATFORM)
    _open(storage, "holder", OwnerType.USER)
    b = TransactionBuilder("MOVE", "test", initiated_at=datetime(2026, 1, 1, 9, 0, 0, 5, tzinfo=JST))
    b.transfer("platform", "holder", Money(cents=7, currency=Currency.JPY))
    ledger.post(b.build(status=TransactionStatus.SETTLED, settled_at=datetime(2025, 12, 31, 19, 0, 1, tzinfo=EST)))
    row = storage._conn.execute("SELECT initiated_at, settled_at FROM transactions").fetchone()
    assert row == ("2026-01-01T00:00:00.000005Z", "2026-01-01T00:00:01.000000Z")
    assert ledger.balance("holder", as_of=T).cents == 0
    assert ledger.balance("holder", as_of=T + timedelta(seconds=1)).cents == 7
    storage.close()


# ---------- naive datetimes are refused ----------


def test_naive_cutoff_is_refused(funded):
    ledger, storage, _tx = funded
    naive = datetime(2026, 1, 1)
    with pytest.raises(NaiveDatetimeError):
        ledger.balance("holder", as_of=naive)
    with pytest.raises(NaiveDatetimeError):
        ledger.all_balances_snapshot(as_of=naive)
    with pytest.raises(NaiveDatetimeError):
        ledger.aggregate_by(OwnerType.USER, Currency.JPY, as_of=naive)
    with pytest.raises(NaiveDatetimeError):
        storage.iter_entries_for_account("holder", naive, (TransactionStatus.SETTLED,))


def test_naive_settlement_time_is_refused_and_nothing_changes(funded):
    ledger, storage, _tx = funded
    b = TransactionBuilder("MOVE", "test", initiated_at=T)
    b.transfer("platform", "holder", Money(cents=5, currency=Currency.JPY))
    ledger.post(b.build())
    with pytest.raises(NaiveDatetimeError):
        ledger.settle(b.transaction_id, settled_at=datetime(2026, 1, 1))
    assert ledger.get_transaction(b.transaction_id).status is TransactionStatus.PENDING
    with pytest.raises(NaiveDatetimeError):
        b.build(status=TransactionStatus.SETTLED, settled_at=datetime(2026, 1, 1))
    assert ledger.balance("holder", as_of=T).cents == 100


def test_naive_start_time_is_refused(funded):
    ledger, storage, _tx = funded
    with pytest.raises(NaiveDatetimeError):
        TransactionBuilder("MOVE", "test", initiated_at=datetime(2026, 1, 1))
    # a transaction object built by hand with a naive start is refused by the ledger and the store
    b = TransactionBuilder("MOVE", "test", initiated_at=T)
    b.transfer("platform", "holder", Money(cents=5, currency=Currency.JPY))
    from dataclasses import replace

    naive_tx = replace(b.build(), initiated_at=datetime(2026, 1, 1))
    with pytest.raises(NaiveDatetimeError):
        ledger.post(naive_tx)
    with pytest.raises(NaiveDatetimeError):
        storage.save_transaction(naive_tx)
    assert storage._conn.execute("SELECT COUNT(*) FROM transactions").fetchone() == (1,)
    assert storage._conn.execute("SELECT COUNT(*) FROM entries").fetchone() == (2,)


def test_naive_account_creation_time_and_reversal_time_are_refused(funded):
    ledger, storage, tx = funded
    with pytest.raises(NaiveDatetimeError):
        _open(storage, "late", OwnerType.USER, created_at=datetime(2026, 1, 1))
    with pytest.raises(NaiveDatetimeError):
        ledger.reverse(tx, "why", "ops", reversed_at=datetime(2026, 1, 2))
    assert storage._conn.execute("SELECT COUNT(*) FROM transactions").fetchone() == (1,)


# ---------- rows written by earlier versions ----------


def test_row_in_the_older_iso_form_is_read_and_compared_as_an_instant(funded):
    ledger, storage, tx = funded
    storage._conn.execute(
        "UPDATE transactions SET initiated_at = ?, settled_at = ? WHERE transaction_id = ?",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", tx),
    )
    assert ledger.balance("holder", as_of=T).cents == 100
    assert ledger.balance("holder", as_of=T - timedelta(seconds=1)).cents == 0
    assert ledger.get_transaction(tx).settled_at == T


def test_row_stored_with_another_offset_is_counted_at_its_instant(funded):
    ledger, storage, tx = funded
    # the same instant as T, as an earlier version would have stored a +09:00 datetime
    storage._conn.execute(
        "UPDATE transactions SET settled_at = ? WHERE transaction_id = ?",
        ("2026-01-01T09:00:00+09:00", tx),
    )
    assert ledger.balance("holder", as_of=T).cents == 100
    assert ledger.balance("holder", as_of=T - timedelta(microseconds=1)).cents == 0
    assert ledger.balance("holder", as_of=datetime(2026, 1, 1, 8, 59, 59, tzinfo=JST)).cents == 0


@pytest.mark.parametrize("column", ["initiated_at", "settled_at"])
def test_row_without_an_offset_is_an_integrity_error_on_read(funded, column):
    ledger, storage, tx = funded
    storage._conn.execute(
        f"UPDATE transactions SET {column} = ? WHERE transaction_id = ?", ("2026-01-01T00:00:00", tx)
    )
    with pytest.raises(IntegrityError):
        ledger.balance("holder", as_of=T)
    with pytest.raises(IntegrityError):
        ledger.get_transaction(tx)
    with pytest.raises(IntegrityError):
        list(ledger.iter_all_transactions())


def test_account_row_without_an_offset_is_an_integrity_error_on_read(funded):
    ledger, storage, _tx = funded
    storage._conn.execute("UPDATE accounts SET created_at = '2026-01-01T00:00:00' WHERE account_id = 'holder'")
    with pytest.raises(IntegrityError):
        ledger.get_account("holder")
    with pytest.raises(IntegrityError):
        ledger.list_accounts()


def test_unreadable_timestamp_text_is_an_integrity_error(funded):
    ledger, storage, tx = funded
    storage._conn.execute("UPDATE transactions SET settled_at = 'yesterday' WHERE transaction_id = ?", (tx,))
    with pytest.raises(IntegrityError):
        ledger.balance("holder", as_of=T)


# ---------- reversal with an explicit time ----------


def test_reverse_with_an_explicit_time_is_reproducible():
    times = []
    for _ in range(2):
        ledger, storage = _ledger()
        _open(storage, "platform", OwnerType.PLATFORM)
        _open(storage, "holder", OwnerType.USER)
        tx = _transfer(ledger, "platform", "holder", 100)
        at = datetime(2026, 1, 2, 9, 0, tzinfo=JST)
        reversal = ledger.reverse(tx, "correction", "ops", reversed_at=at)
        stored = ledger.get_transaction(reversal.transaction_id)
        times.append((stored.initiated_at, stored.settled_at))
        assert stored.status is TransactionStatus.SETTLED
        # the snapshot at the earlier cut-off is unchanged; the correction counts from its instant
        assert ledger.balance("holder", as_of=T).cents == 100
        assert ledger.balance("holder", as_of=at - timedelta(microseconds=1)).cents == 100
        assert ledger.balance("holder", as_of=at).cents == 0
        row = storage._conn.execute(
            "SELECT initiated_at, settled_at FROM transactions WHERE transaction_id = ?",
            (reversal.transaction_id,),
        ).fetchone()
        assert row == ("2026-01-02T00:00:00.000000Z", "2026-01-02T00:00:00.000000Z")
        storage.close()
    assert times[0] == times[1] == (datetime(2026, 1, 2, tzinfo=timezone.utc),) * 2


def test_reverse_without_a_time_still_uses_the_clock(funded):
    ledger, _storage, tx = funded
    reversal = ledger.reverse(tx, "correction", "ops")
    assert reversal.settled_at is not None and reversal.settled_at.utcoffset() == timedelta(0)
    assert reversal.settled_at > T


# ---------- pending and failed transactions ----------


def test_pending_transaction_is_never_in_the_settled_balance(funded):
    ledger, _storage, _tx = funded
    pending = _transfer(ledger, "platform", "holder", 40, initiated_at=T - timedelta(days=1), settled_at=None)
    far = T + timedelta(days=365)
    assert ledger.balance("holder", as_of=far).cents == 100
    # asked for explicitly, a pending transaction counts from its start instant
    assert ledger.balance("holder", as_of=far, include_pending=True).cents == 140
    assert ledger.balance("holder", as_of=T - timedelta(days=1), include_pending=True).cents == 40
    assert ledger.balance("holder", as_of=T - timedelta(days=1, microseconds=1), include_pending=True).cents == 0
    assert ledger.balance(
        "holder", as_of=datetime(2025, 12, 31, 9, 0, tzinfo=JST), include_pending=True
    ).cents == 40
    ledger.fail_transaction(pending, "rejected")
    assert ledger.balance("holder", as_of=far, include_pending=True).cents == 100


def test_settled_transaction_without_a_settlement_instant_is_not_counted():
    ledger, storage = _ledger()
    _open(storage, "platform", OwnerType.PLATFORM)
    _open(storage, "holder", OwnerType.USER)
    b = TransactionBuilder("MOVE", "test", initiated_at=T)
    b.transfer("platform", "holder", Money(cents=9, currency=Currency.JPY))
    ledger.post(b.build(status=TransactionStatus.SETTLED, settled_at=None))
    assert ledger.balance("holder", as_of=T + timedelta(days=1)).cents == 0
    storage.close()


# ---------- ordering is by instant, never by text ----------


def test_accounts_are_listed_by_creation_instant_then_insertion_order():
    ledger, storage = _ledger()
    # Text order of the stored values would differ from time order before normalisation:
    # "2026-01-01T08:30:00+09:00" (2025-12-31T23:30Z) sorts after "2026-01-01T00:00:00+00:00".
    _open(storage, "second", OwnerType.USER, created_at=T)
    _open(storage, "first", OwnerType.USER, created_at=datetime(2026, 1, 1, 8, 30, tzinfo=JST))
    _open(storage, "third-a", OwnerType.USER, created_at=T + timedelta(hours=1))
    _open(storage, "third-b", OwnerType.USER, created_at=datetime(2025, 12, 31, 20, 0, tzinfo=EST))
    assert [a.account_id for a in ledger.list_accounts()] == ["first", "second", "third-a", "third-b"]
    assert [a.account_id for a in ledger.list_accounts(owner_type=OwnerType.USER)] == [
        "first", "second", "third-a", "third-b",
    ]
    # a row in the older form still sorts by its instant
    storage._conn.execute("UPDATE accounts SET created_at = '2026-01-01T12:00:00+09:00' WHERE account_id = 'first'")
    assert [a.account_id for a in ledger.list_accounts()] == ["second", "third-a", "third-b", "first"]
    storage.close()


def test_transactions_are_iterated_by_start_instant():
    ledger, storage = _ledger()
    _open(storage, "platform", OwnerType.PLATFORM)
    _open(storage, "holder", OwnerType.USER)
    late = _transfer(ledger, "platform", "holder", 1, initiated_at=T + timedelta(hours=2), settled_at=T + timedelta(hours=2))
    early = _transfer(ledger, "platform", "holder", 2, initiated_at=datetime(2026, 1, 1, 9, 30, tzinfo=JST), settled_at=T + timedelta(hours=3))
    same_a = _transfer(ledger, "platform", "holder", 3, initiated_at=T + timedelta(hours=1), settled_at=T + timedelta(hours=3))
    same_b = _transfer(ledger, "platform", "holder", 4, initiated_at=datetime(2026, 1, 1, 10, 0, tzinfo=JST), settled_at=T + timedelta(hours=3))
    assert [tx.transaction_id for tx in ledger.iter_all_transactions()] == [early, same_a, same_b, late]
    entries = list(storage.iter_entries_for_account("holder", T + timedelta(days=1), (TransactionStatus.SETTLED,)))
    assert [e.amount.cents for e in entries] == [2, 3, 4, 1]
    storage.close()


def test_the_store_does_not_filter_or_order_by_time_in_sql():
    import ast
    import re

    tree = ast.parse((REPO / "openreserve" / "core" / "storage.py").read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    sql = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and re.search(r"\b(SELECT|UPDATE|INSERT|WHERE|ORDER BY|JOIN)\b", node.value)
    ]
    assert len(sql) >= 8, "the SQL statements of the store were not found"
    for statement in sql:
        assert not re.search(r"ORDER BY[^;]*_at\b", statement), statement
        assert not re.search(r"_at\s*(<|>|<=|>=|BETWEEN)", statement), statement
