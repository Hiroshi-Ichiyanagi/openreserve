"""Amounts as canonical decimal text in the store, schema versions, and the migration of
0.1.x files (design section 9.1).

The 0.1.x file used here is built inside the test from the CREATE TABLE statements of that
version, kept below as a fixture, with rows written the way that version wrote them.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import openreserve.core.migration as migration_mod
from openreserve.conversion import (
    close_conversion,
    convert,
    load_declaration,
    post_conversion_batch,
)
from openreserve.core import migrate_ledger_database as exported_migrate
from openreserve.core.exact import (
    AMOUNT_MAX,
    INSTANT_MAX,
    ExactValueError,
    NaiveDatetimeError,
    amount_from_text,
    instant_from_text,
)
from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.migration import MigrationError, migrate_ledger_database
from openreserve.core.storage import AMOUNT_CHECK, SCHEMA_VERSION, SchemaVersionError, SQLiteLedgerStorage
from openreserve.core.types import Account, Currency, IntegrityError, Money, OwnerType
from openreserve.transparency.audit_log import AuditLog
from openreserve.transparency.snapshot import LiabilitiesSnapshotGenerator

T = datetime(2026, 1, 1, tzinfo=timezone.utc)
APPLIED = datetime(2026, 11, 17, tzinfo=timezone.utc)
VECTORS = Path(__file__).parent / "vectors"

# The schema of openreserve 0.1.x, verbatim (fixture for the migration tests).
LEGACY_SCHEMA_V0 = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    label TEXT NOT NULL,
    regulatory_tags TEXT NOT NULL,  -- JSON array
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_owner_type ON accounts(owner_type);
CREATE INDEX IF NOT EXISTS idx_accounts_currency ON accounts(currency);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    purpose_code TEXT NOT NULL,
    initiator_id TEXT NOT NULL,
    initiated_at TEXT NOT NULL,
    settled_at TEXT,
    status TEXT NOT NULL,
    external_refs TEXT NOT NULL,    -- JSON array of [provider, ref] pairs
    metadata TEXT NOT NULL,         -- JSON array of [key, value] pairs
    compliance_decision TEXT        -- JSON object or NULL
);

CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_initiated_at ON transactions(initiated_at);
CREATE INDEX IF NOT EXISTS idx_transactions_settled_at ON transactions(settled_at);

CREATE TABLE IF NOT EXISTS entries (
    entry_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_account_id ON entries(account_id);
CREATE INDEX IF NOT EXISTS idx_entries_transaction_id ON entries(transaction_id);
"""

LEGACY_ACCOUNTS = [
    ("platform", "PLATFORM", "JPYC", "2026-01-01T00:00:00+00:00"),
    ("u", "USER", "JPYC", "2026-01-01T00:00:00+00:00"),
    ("v", "USER", "JPYC", "2026-01-01T09:00:00+09:00"),
]
# (transaction id, source, destination, amount, initiated_at, settled_at)
LEGACY_TRANSFERS = [
    ("t1", "platform", "u", 9 * 10**18, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    # Settled at the same instant as T, written with +09:00: 0.1.x compared this text with
    # the cut-off text and left it out of the balance at T.
    ("t2", "platform", "v", 2**63 - 1, "2026-01-01T00:00:00+00:00", "2026-01-01T09:00:00+09:00"),
    ("t3", "u", "v", 5, "2026-01-01T00:00:00.000007+00:00", "2026-01-01T00:00:01.500000+00:00"),
    ("t4", "platform", "u", 123, "2026-01-01T00:00:02+00:00", None),  # pending
]


def make_legacy_file(path: Path, *, accounts=LEGACY_ACCOUNTS, transfers=LEGACY_TRANSFERS, extra=None) -> None:
    """A ledger file as 0.1.x wrote it: user_version 0, integer amounts, isoformat text."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(LEGACY_SCHEMA_V0)
    for account_id, owner_type, currency, created_at in accounts:
        conn.execute(
            "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)",
            (account_id, owner_type, currency, account_id, "[]", created_at),
        )
    for transaction_id, src, dst, amount, initiated_at, settled_at in transfers:
        conn.execute(
            "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (transaction_id, "MOVE", "test", initiated_at, settled_at, "SETTLED" if settled_at else "PENDING",
             "[]", "[]", None),
        )
        currency = "JPYC"
        conn.execute("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)", (f"{transaction_id}:0", transaction_id, src, -amount, currency, 0))
        conn.execute("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)", (f"{transaction_id}:1", transaction_id, dst, amount, currency, 1))
    if extra is not None:
        extra(conn)
    conn.close()


def dump(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump()) + [f"user_version={conn.execute('PRAGMA user_version').fetchone()[0]}"]
    finally:
        conn.close()


def user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    make_legacy_file(path)
    return path


class Store:
    """A ledger on a fresh store (a new SQLite database in RAM by default), accounts of fixed ids."""

    def __init__(self, path: str = ":memory:") -> None:
        self.storage = SQLiteLedgerStorage(path)
        self.ledger = Ledger(self.storage)

    def open(self, account_id: str, owner_type: OwnerType = OwnerType.USER, currency: Currency = Currency.JPYC) -> str:
        self.storage.save_account(
            Account(account_id=account_id, owner_type=owner_type, currency=currency, label=account_id, created_at=T)
        )
        return account_id

    def move(self, src: str, dst: str, amount: int, currency: Currency = Currency.JPYC, at: datetime = T) -> str:
        """``-amount`` to ``src`` and ``+amount`` to ``dst``; any sign, zero included."""
        b = TransactionBuilder("MOVE", "test", initiated_at=at)
        b.add_entry(src, Money(cents=-amount, currency=currency))
        b.add_entry(dst, Money(cents=amount, currency=currency))
        self.ledger.post(b.build())
        self.ledger.settle(b.transaction_id, settled_at=at)
        return b.transaction_id

    def balance(self, account_id: str, at: datetime = INSTANT_MAX) -> int:
        return self.ledger.balance(account_id, as_of=at).cents

    def raw_amounts(self) -> list[tuple[object, str]]:
        return self.storage._conn.execute("SELECT amount, typeof(amount) FROM entries ORDER BY rowid").fetchall()


# ---------------------------------------------------------------------------
# Amounts through the store
# ---------------------------------------------------------------------------

ROUND_TRIP = [0, 1, -1, 2**63 - 1, -(2**63 - 1), 2**63, -(2**63), 10**30, AMOUNT_MAX, -AMOUNT_MAX]


@pytest.mark.parametrize("amount", ROUND_TRIP)
def test_amount_round_trips_exactly_through_the_store(amount):
    s = Store()
    s.open("a")
    s.open("b")
    transaction_id = s.move("a", "b", amount)
    loaded = s.ledger.get_transaction(transaction_id)
    assert [entry.amount.cents for entry in loaded.entries] == [-amount, amount]
    assert all(type(entry.amount.cents) is int for entry in loaded.entries)
    assert (s.balance("a"), s.balance("b")) == (-amount, amount)
    assert s.balance("b", at=T) == amount
    assert s.raw_amounts() == [(str(-amount), "text"), (str(amount), "text")]


@pytest.mark.parametrize("amount", [10**38, -(10**38), 10**40])
def test_amount_beyond_the_bound_is_refused_before_anything_is_written(amount):
    s = Store()
    s.open("a")
    s.open("b")
    b = TransactionBuilder("MOVE", "test", initiated_at=T)
    b.add_entry("a", Money(cents=-amount, currency=Currency.JPYC))
    b.add_entry("b", Money(cents=amount, currency=Currency.JPYC))
    with pytest.raises(ExactValueError):
        s.ledger.post(b.build())
    conn = s.storage._conn
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert not conn.in_transaction


def test_balance_of_a_thousand_18_decimal_entries_is_the_exact_sum():
    s = Store()
    s.open("platform", OwnerType.PLATFORM)
    s.open("holder")
    amounts = [(i + 1) * 10**18 + (7**i % 10**18) for i in range(1000)]
    for amount in amounts:
        s.move("platform", "holder", amount)
    exact = sum(amounts)
    assert exact > 2**63 * 1000  # far beyond any 64-bit or float-exact value
    assert s.balance("holder") == exact
    assert s.balance("platform") == -exact
    assert s.balance("holder", at=T) == exact
    assert s.balance("holder", at=T - timedelta(microseconds=1)) == 0


def _insert_entry_by_hand(storage: SQLiteLedgerStorage, amount) -> None:
    conn = storage._conn
    conn.execute(
        "INSERT INTO entries (entry_id, transaction_id, account_id, amount, currency, sequence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"hand-{amount!r}", "hand", "a", amount, "JPYC", 0),
    )


def _store_with_a_hand_transaction() -> Store:
    s = Store()
    s.open("a")
    s.storage._conn.execute(
        "INSERT INTO transactions VALUES ('hand', 'MOVE', 'test', ?, ?, 'SETTLED', '[]', '[]', NULL)",
        ("2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z"),
    )
    return s


@pytest.mark.parametrize("amount", ["-0", "01", "1.0", " 1", "1 ", "1\n", "+1", "--1", "1-2", "", "-", "１", "9" * 39, "-" + "9" * 39, 1.5, 1e3, b"1",
                                    "1\x000", "1\x00", "\x001", "0\x00", "-5\x00abc", "9" * 38 + "\x00"])
def test_column_check_refuses_a_non_canonical_amount_written_by_hand(amount):
    s = _store_with_a_hand_transaction()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_entry_by_hand(s.storage, amount)


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
def test_column_check_agrees_with_the_amount_text_rule(encoding):
    # SQLite's GLOB, length and ltrim stop at the first NUL of a text; the column check must
    # still accept exactly the canonical texts, whatever the database encoding.
    conn = sqlite3.connect(":memory:")
    conn.execute(f"PRAGMA encoding = '{encoding}'")
    conn.execute(f"CREATE TABLE e (amount TEXT NOT NULL CHECK ({AMOUNT_CHECK}))")
    assert conn.execute("PRAGMA encoding").fetchone()[0] == encoding
    rng = random.Random(5)
    alphabet = ["0", "1", "9", "-", "+", " ", ".", "e", "\n", "\x00", "１", "a", "٣"]
    texts = ["0", "1", "-1", "9" * 38, "-" + "9" * 38, "1\x000", "-5\x00abc", "\x00"]
    texts += ["".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 7))) for _ in range(5000)]
    disagreements = []
    for text in texts:
        try:
            conn.execute("INSERT INTO e VALUES (?)", (text,))
            stored = True
        except sqlite3.IntegrityError:
            stored = False
        try:
            amount_from_text(text)
            canonical = True
        except ExactValueError:
            canonical = False
        if stored != canonical:
            disagreements.append(text)
    assert disagreements == []
    assert any("\x00" in text for text in texts)


def test_integer_written_by_hand_is_stored_as_its_text():
    s = _store_with_a_hand_transaction()
    _insert_entry_by_hand(s.storage, 5)
    _insert_entry_by_hand(s.storage, "9" * 38)
    assert s.raw_amounts() == [("5", "text"), ("9" * 38, "text")]
    assert s.balance("a") == 5 + int("9" * 38)


def test_non_canonical_amount_in_a_file_is_an_integrity_error_on_read():
    s = _store_with_a_hand_transaction()
    s.storage._conn.execute("PRAGMA ignore_check_constraints = ON")
    _insert_entry_by_hand(s.storage, "007")
    s.storage._conn.execute("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(IntegrityError, match="Unreadable amount"):
        s.balance("a")
    with pytest.raises(IntegrityError, match="Unreadable amount"):
        s.ledger.get_transaction("hand")


# ---------------------------------------------------------------------------
# Schema versions
# ---------------------------------------------------------------------------


def test_new_file_empty_file_and_ram_store_have_schema_version_1(tmp_path: Path):
    assert SCHEMA_VERSION == 1
    assert SQLiteLedgerStorage(":memory:").schema_version == 1
    path = tmp_path / "new.db"
    SQLiteLedgerStorage(str(path)).close()
    assert user_version(path) == 1
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    s = Store(str(empty))
    s.open("a")
    s.storage.close()
    assert user_version(empty) == 1
    again = SQLiteLedgerStorage(str(empty))  # a version 1 file opens as it is
    assert [a.account_id for a in again.list_accounts()] == ["a"]
    again.close()


def test_version_0_file_with_data_is_refused_and_names_the_migration(legacy: Path):
    before = dump(legacy)
    with pytest.raises(SchemaVersionError, match="migrate_ledger_database") as info:
        SQLiteLedgerStorage(str(legacy))
    assert isinstance(info.value, IntegrityError)
    assert dump(legacy) == before  # nothing was converted silently


def test_version_0_file_with_empty_ledger_tables_is_refused_too(tmp_path: Path):
    path = tmp_path / "empty-tables.db"
    make_legacy_file(path, accounts=[], transfers=[])
    with pytest.raises(SchemaVersionError, match="migrate_ledger_database"):
        SQLiteLedgerStorage(str(path))
    assert user_version(path) == 0


def test_file_of_a_newer_version_is_refused(tmp_path: Path):
    path = tmp_path / "newer.db"
    SQLiteLedgerStorage(str(path)).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    with pytest.raises(SchemaVersionError, match="newer"):
        SQLiteLedgerStorage(str(path))


def test_file_that_says_version_1_with_the_old_entries_table_is_refused(tmp_path: Path):
    path = tmp_path / "relabelled.db"
    make_legacy_file(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with pytest.raises(SchemaVersionError, match="not the one of that version"):
        SQLiteLedgerStorage(str(path))


def test_version_0_file_with_only_other_tables_gets_the_ledger_schema(tmp_path: Path):
    path = tmp_path / "shared.db"
    audit = AuditLog(str(path))
    audit.append("event", {"n": 1}, timestamp=T)
    audit.close()
    s = Store(str(path))
    s.open("a")
    assert s.storage.schema_version == 1
    s.storage.close()


# ---------------------------------------------------------------------------
# Migration of a 0.1.x file
# ---------------------------------------------------------------------------


def test_migration_keeps_amounts_and_balances_and_fixes_the_offset_cutoff(legacy: Path, tmp_path: Path):
    conn = sqlite3.connect(legacy)
    legacy_rows = conn.execute("SELECT account_id, amount_cents, typeof(amount_cents) FROM entries ORDER BY rowid").fetchall()
    conn.close()
    assert {row[2] for row in legacy_rows} == {"integer"}

    result = migrate_ledger_database(legacy, backup_path=tmp_path / "ledger.backup.db", applied_at=APPLIED)
    assert result == {
        "from_version": 0,
        "to_version": 1,
        "accounts": 3,
        "transactions": 4,
        "entries": 8,
        "timestamps_rewritten": 10,
        "timestamps_assumed_utc": 0,
        "naive_timestamps": "refuse",
        "applied_at": "2026-11-17T00:00:00.000000Z",
        "backup_path": str(tmp_path / "ledger.backup.db"),
    }
    assert user_version(legacy) == 1

    s = Store(str(legacy))
    conn = s.storage._conn
    assert conn.execute("SELECT account_id, amount FROM entries ORDER BY rowid").fetchall() == [
        (account_id, str(amount)) for account_id, amount, _ in legacy_rows
    ]
    assert {row[0] for row in conn.execute("SELECT typeof(amount) FROM entries")} == {"text"}
    # The balances over all time are the Python sums of the legacy integers.
    expected: dict[str, int] = {}
    for account_id, amount, _ in legacy_rows:
        if account_id == "u" and amount == 123:
            continue  # the pending transfer is not a settled balance
        expected[account_id] = expected.get(account_id, 0) + amount
    expected["platform"] += 123  # its pending debit does not count either
    assert {a: s.balance(a) for a in ("platform", "u", "v")} == expected
    # The settlement written with +09:00 is the instant T and is counted at the cut-off T.
    assert s.balance("v", at=T) == 2**63 - 1
    assert s.balance("u", at=T) == 9 * 10**18
    assert s.ledger.balance("u", as_of=INSTANT_MAX, include_pending=True).cents == 9 * 10**18 - 5 + 123
    # Every timestamp is canonical UTC text now.
    stamps = conn.execute("SELECT created_at FROM accounts").fetchall() + conn.execute(
        "SELECT initiated_at FROM transactions UNION ALL SELECT settled_at FROM transactions WHERE settled_at IS NOT NULL"
    ).fetchall()
    assert all(len(text) == 27 and text.endswith("Z") for (text,) in stamps)
    assert conn.execute("SELECT settled_at FROM transactions WHERE transaction_id = 't2'").fetchone() == (
        "2026-01-01T00:00:00.000000Z",
    )
    # The step is recorded with the given instant and canonical details.
    version, applied, details = conn.execute("SELECT version, applied_at, details FROM schema_migrations").fetchone()
    assert (version, applied) == (1, "2026-11-17T00:00:00.000000Z")
    assert json.loads(details) == {
        "accounts": "3",
        "entries": "8",
        "from_version": "0",
        "naive_timestamps": "refuse",
        "timestamps_assumed_utc": "0",
        "timestamps_rewritten": "10",
        "to_version": "1",
        "transactions": "4",
    }
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'entries'")}
    assert {"idx_entries_account_id", "idx_entries_transaction_id"} <= indexes
    # the migrated entries table has the column check of a new file, NUL clause included
    entries_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entries'").fetchone()[0]
    assert f"CHECK ({AMOUNT_CHECK})" in entries_sql and "instr(amount, char(0)) = 0" in entries_sql

    # Ten units of the 18-decimal currency (10**19 minor units) can be posted afterwards.
    s.move("platform", "u", 10**19, at=T + timedelta(seconds=3))
    assert s.balance("u") == 9 * 10**18 - 5 + 10**19
    s.storage.close()


def test_snapshot_of_a_migrated_file_equals_that_of_a_ledger_written_by_this_version(legacy: Path, tmp_path: Path):
    migrate_ledger_database(legacy, backup_path=tmp_path / "b.db", applied_at=APPLIED)
    migrated = Store(str(legacy))
    native = Store()
    for account_id, owner_type, _currency, _created in LEGACY_ACCOUNTS:
        native.open(account_id, OwnerType(owner_type))
    for _tid, src, dst, amount, initiated_at, settled_at in LEGACY_TRANSFERS:
        b = TransactionBuilder("MOVE", "test", initiated_at=datetime.fromisoformat(initiated_at))
        b.transfer(src, dst, Money(cents=amount, currency=Currency.JPYC))
        native.ledger.post(b.build())
        if settled_at is not None:
            native.ledger.settle(b.transaction_id, settled_at=datetime.fromisoformat(settled_at))
    for at in (T, T + timedelta(seconds=2)):
        a, b = (
            LiabilitiesSnapshotGenerator(s.ledger, institution_id="example-institution", salt_key=bytes(range(32))).generate(Currency.JPYC, at)
            for s in (migrated, native)
        )
        assert a.summary_hash() == b.summary_hash()
        assert a.liabilities.total == 9 * 10**18 + 2**63 - 1  # above 2**63 - 1, exact
        assert a.liabilities.leaf_count == 2
    migrated.storage.close()


def test_backup_equals_the_original_content(legacy: Path, tmp_path: Path):
    before = dump(legacy)
    backup = tmp_path / "copy.db"
    migrate_ledger_database(legacy, backup_path=backup, applied_at=APPLIED)
    assert dump(backup) == before
    assert user_version(backup) == 0
    assert dump(legacy) != before


def test_second_run_and_existing_backup_path_are_refused(legacy: Path, tmp_path: Path):
    existing = tmp_path / "exists.db"
    existing.write_bytes(b"keep")
    before = dump(legacy)
    with pytest.raises(FileExistsError):
        migrate_ledger_database(legacy, backup_path=existing, applied_at=APPLIED)
    assert existing.read_bytes() == b"keep"
    assert dump(legacy) == before

    migrate_ledger_database(legacy, backup_path=tmp_path / "first.db", applied_at=APPLIED)
    after = dump(legacy)
    with pytest.raises(SchemaVersionError, match="version 1"):
        migrate_ledger_database(legacy, backup_path=tmp_path / "second.db", applied_at=APPLIED)
    assert not (tmp_path / "second.db").exists()
    assert dump(legacy) == after


def test_real_amount_stops_the_migration_and_leaves_the_file_unchanged(tmp_path: Path):
    path = tmp_path / "real.db"

    def add_text_amount(conn):
        # 0.1.x accepted this: integer text above 2**63 - 1 in an INTEGER column becomes REAL.
        conn.execute("INSERT INTO entries VALUES ('bad:0', 't1', 'u', '10000000000000000000', 'JPYC', 2)")

    make_legacy_file(path, extra=add_text_amount)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT typeof(amount_cents) FROM entries WHERE entry_id = 'bad:0'").fetchone() == ("real",)
    conn.close()
    before = dump(path)
    with pytest.raises(MigrationError, match=r"bad:0.*real"):
        migrate_ledger_database(path, backup_path=tmp_path / "b.db", applied_at=APPLIED)
    assert dump(path) == before
    assert user_version(path) == 0


def test_naive_timestamp_stops_the_migration_unless_utc_is_stated(tmp_path: Path):
    path = tmp_path / "naive.db"
    accounts = LEGACY_ACCOUNTS[:2] + [("v", "USER", "JPYC", "2026-01-01T00:00:00")]
    make_legacy_file(path, accounts=accounts)
    before = dump(path)
    with pytest.raises(MigrationError, match=r"accounts\.created_at.*no UTC offset.*assume-utc"):
        migrate_ledger_database(path, backup_path=tmp_path / "b1.db", applied_at=APPLIED)
    assert dump(path) == before
    assert user_version(path) == 0

    result = migrate_ledger_database(
        path, backup_path=tmp_path / "b2.db", applied_at=APPLIED, naive_timestamps="assume-utc"
    )
    assert (result["timestamps_assumed_utc"], result["naive_timestamps"]) == (1, "assume-utc")
    assert user_version(path) == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT created_at FROM accounts WHERE account_id = 'v'").fetchone() == (
        "2026-01-01T00:00:00.000000Z",
    )
    conn.close()


def test_backup_of_a_stopped_migration_stays_and_is_named_in_the_error(tmp_path: Path):
    path = tmp_path / "naive.db"
    accounts = LEGACY_ACCOUNTS[:2] + [("v", "USER", "JPYC", "2026-01-01T00:00:00")]
    make_legacy_file(path, accounts=accounts)
    before = dump(path)
    backup = tmp_path / "b1.db"
    with pytest.raises(MigrationError) as stopped:
        migrate_ledger_database(path, backup_path=backup, applied_at=APPLIED)
    message = str(stopped.value)
    assert "no UTC offset" in message
    assert repr(str(backup)) in message and "complete copy of the unchanged file" in message
    # the file is unchanged and the backup is a complete copy of it
    assert dump(path) == before and user_version(path) == 0
    assert dump(backup) == before and user_version(backup) == 0
    # so the same backup path is refused on the next run; another one works
    with pytest.raises(FileExistsError):
        migrate_ledger_database(path, backup_path=backup, applied_at=APPLIED, naive_timestamps="assume-utc")
    migrate_ledger_database(path, backup_path=tmp_path / "b2.db", applied_at=APPLIED, naive_timestamps="assume-utc")
    assert user_version(path) == 1 and dump(backup) == before


def test_errors_before_the_backup_leave_no_backup_file(legacy: Path, tmp_path: Path):
    backup = tmp_path / "b.db"
    with pytest.raises(NaiveDatetimeError):
        migrate_ledger_database(legacy, backup_path=backup, applied_at=datetime(2026, 11, 17))
    with pytest.raises(ValueError):
        migrate_ledger_database(legacy, backup_path=backup, applied_at=APPLIED, naive_timestamps="utc")
    with pytest.raises(FileNotFoundError):
        migrate_ledger_database(tmp_path / "missing.db", backup_path=backup, applied_at=APPLIED)
    new = tmp_path / "new.db"
    SQLiteLedgerStorage(str(new)).close()
    with pytest.raises(SchemaVersionError):
        migrate_ledger_database(new, backup_path=backup, applied_at=APPLIED)
    assert not backup.exists()


def test_unreadable_timestamp_stops_the_migration(tmp_path: Path):
    path = tmp_path / "garbage.db"
    transfers = LEGACY_TRANSFERS[:1] + [("t9", "platform", "u", 1, "yesterday", "2026-01-01T00:00:00+00:00")]
    make_legacy_file(path, transfers=transfers)
    before = dump(path)
    with pytest.raises(MigrationError, match=r"transactions\.initiated_at.*'t9'.*yesterday"):
        migrate_ledger_database(path, backup_path=tmp_path / "b.db", applied_at=APPLIED, naive_timestamps="assume-utc")
    assert dump(path) == before


def test_the_audit_log_in_the_same_file_is_not_touched(tmp_path: Path):
    path = tmp_path / "with-audit.db"
    make_legacy_file(path)
    audit = AuditLog(str(path))
    audit.append("event", {"n": 1}, timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=9))))
    audit.close()
    conn = sqlite3.connect(path)
    events_before = conn.execute("SELECT * FROM audit_events ORDER BY rowid").fetchall()
    conn.close()
    migrate_ledger_database(path, backup_path=tmp_path / "b.db", applied_at=APPLIED)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT * FROM audit_events ORDER BY rowid").fetchall() == events_before
    conn.close()
    audit = AuditLog(str(path))
    assert audit.verify_chain()[0]
    audit.close()


def test_arguments_are_checked_before_anything_is_written(legacy: Path, tmp_path: Path):
    before = dump(legacy)
    with pytest.raises(NaiveDatetimeError):
        migrate_ledger_database(legacy, backup_path=tmp_path / "b.db", applied_at=datetime(2026, 11, 17))
    with pytest.raises(ValueError, match="naive_timestamps"):
        migrate_ledger_database(legacy, backup_path=tmp_path / "b.db", applied_at=APPLIED, naive_timestamps="utc")
    with pytest.raises(FileNotFoundError):
        migrate_ledger_database(tmp_path / "missing.db", backup_path=tmp_path / "b.db", applied_at=APPLIED)
    assert not (tmp_path / "missing.db").exists()
    assert not (tmp_path / "b.db").exists()
    assert dump(legacy) == before


def test_files_that_are_not_version_0_ledger_files_are_refused(tmp_path: Path):
    new = tmp_path / "new.db"
    SQLiteLedgerStorage(str(new)).close()
    with pytest.raises(SchemaVersionError, match="version 1"):
        migrate_ledger_database(new, backup_path=tmp_path / "b1.db", applied_at=APPLIED)
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE something (x)")
    conn.close()
    with pytest.raises(SchemaVersionError, match="not a version 0 ledger file"):
        migrate_ledger_database(other, backup_path=tmp_path / "b2.db", applied_at=APPLIED)
    assert not (tmp_path / "b1.db").exists() and not (tmp_path / "b2.db").exists()


def test_a_writer_between_backup_and_migration_stops_it(legacy: Path, tmp_path: Path, monkeypatch):
    real_connect = sqlite3.connect

    class Source:
        """Delegates to the real connection; after the backup another connection writes."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def backup(self, target):
            self._conn.backup(target)
            other = real_connect(legacy, isolation_level=None)
            other.execute("UPDATE accounts SET label = 'changed' WHERE account_id = 'u'")
            other.close()

    def connect(path, *args, **kwargs):
        conn = real_connect(path, *args, **kwargs)
        return Source(conn) if os.fspath(path) == os.fspath(legacy) else conn

    monkeypatch.setattr(migration_mod.sqlite3, "connect", connect)
    with pytest.raises(MigrationError, match="another connection changed the file"):
        migrate_ledger_database(legacy, backup_path=tmp_path / "b.db", applied_at=APPLIED)
    monkeypatch.undo()
    assert user_version(legacy) == 0
    conn = sqlite3.connect(legacy)
    assert conn.execute("PRAGMA table_info(entries)").fetchall()[3][1] == "amount_cents"
    conn.close()


def test_migration_is_exported_from_the_core_package():
    assert exported_migrate is migrate_ledger_database


# ---------------------------------------------------------------------------
# 18-decimal amounts above 2**63 through a declared conversion in the ledger
# ---------------------------------------------------------------------------

CONVERSION_CASES = {c["name"]: c for c in json.loads((VECTORS / "conversion_v1.json").read_text(encoding="utf-8"))["cases"]}


def _stand_in_declaration(case_name: str, *, source: Currency, target: Currency) -> dict:
    """The vector declaration with ledger currencies in place of the fictional code. The
    arithmetic depends only on the rate, the minor units and the rounding mode."""
    document = json.loads(json.dumps(CONVERSION_CASES[case_name]["declaration"]))
    for side, currency in (("source", source), ("target", target)):
        assert document[side]["minor_units"] == str(currency.minor_units)
        document[side]["code"] = currency.code
    document["declaration_id"] = f"stand-in/{source.code}-{target.code}"
    return document


@pytest.mark.parametrize(
    "case_name, source, target",
    [
        ("jpyc18-to-fictional-w2", Currency.JPYC, Currency.USD),
        ("usd2-to-fictional-w18", Currency.USD, Currency.JPYC),
    ],
)
def test_conversion_of_18_decimal_amounts_above_2_63_through_the_ledger(case_name, source, target):
    declaration = load_declaration(_stand_in_declaration(case_name, source=source, target=target))
    vectors = CONVERSION_CASES[case_name]["conversions"]
    effective = declaration.cutoff + timedelta(seconds=1)
    s = Store()
    s.open("platform-src", OwnerType.PLATFORM, source)
    s.open("platform-dst", OwnerType.PLATFORM, target)
    s.open("clearing-src", OwnerType.CLEARING, source)
    s.open("clearing-dst", OwnerType.CLEARING, target)
    s.open("residue", OwnerType.FX_GAIN_LOSS, target)
    pairs = [("platform-src", "platform-dst")]
    for i, vector in enumerate(vectors):
        x = int(vector["x"])
        src, dst = s.open(f"h{i:02d}-src", currency=source), s.open(f"h{i:02d}-dst", currency=target)
        if x > 0:
            s.move("platform-src", src, x, currency=source)
        elif x < 0:
            s.move(src, "platform-src", -x, currency=source)  # an overdrawn holder
        pairs.append((src, dst))

    result = post_conversion_batch(
        s.ledger, declaration, pairs=pairs, clearing_source_account_id="clearing-src",
        clearing_target_account_id="clearing-dst", effective_at=effective, initiator_id="ops",
    )
    assert len(result.posted) == len(pairs) - sum(1 for v in vectors if v["x"] == "0")

    for i, vector in enumerate(vectors):
        x, y = int(vector["x"]), int(vector["y"])
        converted = convert(declaration, x)
        assert (converted.exact, converted.converted_amount, converted.residue) == (
            Fraction(int(vector["z"]["num"]), int(vector["z"]["den"])),
            y,
            Fraction(int(vector["e"]["num"]), int(vector["e"]["den"])),
        )
        assert s.balance(f"h{i:02d}-src") == 0
        assert s.balance(f"h{i:02d}-dst") == y
    assert any(abs(int(v["x"])) > 2**63 or abs(int(v["y"])) > 2**63 for v in vectors)

    assert s.balance("platform-src") == 0  # every source balance moved to the clearing account
    total_y = sum(s.balance(account_id) for _src, account_id in pairs)
    residue = close_conversion(
        s.ledger, declaration, clearing_source_account_id="clearing-src", clearing_target_account_id="clearing-dst",
        residue_account_id="residue", effective_at=effective, initiator_id="ops",
    )
    assert s.balance("clearing-src") == 0
    assert s.balance("clearing-dst") == 0
    assert s.balance("residue") == residue == -total_y
    assert sum(s.balance(a) for a in ("platform-dst", "clearing-dst", "residue")) + sum(
        s.balance(dst) for _src, dst in pairs[1:]
    ) == 0
    # The large values went through the store as text.
    assert any(len(text.lstrip("-")) > 19 for text, kind in s.raw_amounts() if kind == "text")
    assert {kind for _text, kind in s.raw_amounts()} == {"text"}
