"""
core/storage.py — SQLiteベースの永続化層。

ACIDトランザクション、WALモード、append-only制約を実装する。
SQLiteはM2 Pro 16GBで十分なパフォーマンスを発揮し、
シングルファイルでの可搬性が高いため、検証段階に最適。

本番でPostgreSQLに移行する際は、このモジュールを差し替えるだけで済むよう、
LedgerStorageインターフェースを抽象化しておく。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Iterator, Protocol

from openreserve.core.exact import (
    ExactValueError,
    amount_from_text,
    amount_to_text,
    instant_from_stored_text,
    instant_to_text,
    require_aware,
)
from openreserve.core.types import (
    Account,
    ComplianceDecision,
    Currency,
    Entry,
    IntegrityError,
    Money,
    OwnerType,
    Transaction,
    TransactionStatus,
)


# --------- 抽象インターフェース ---------


class LedgerStorage(Protocol):
    def save_account(self, account: Account) -> None: ...
    def load_account(self, account_id: str) -> Account: ...
    def list_accounts(self, owner_type: OwnerType | None = None) -> list[Account]: ...
    def save_transaction(self, transaction: Transaction) -> None: ...
    def load_transaction(self, transaction_id: str) -> Transaction: ...
    def update_transaction_status(self, transaction: Transaction) -> None: ...
    def iter_entries_for_account(
        self,
        account_id: str,
        as_of: datetime,
        statuses: tuple[TransactionStatus, ...],
    ) -> Iterator[Entry]: ...
    def iter_all_transactions(self) -> Iterator[Transaction]: ...


# --------- SQLite実装 ---------


#: Schema version of the files this module writes, kept in ``PRAGMA user_version``. Files of
#: 0.1.x have version 0; they are converted only by an explicit call of
#: :func:`openreserve.core.migration.migrate_ledger_database`.
SCHEMA_VERSION = 1

#: Second line of defence for the amount column: canonical decimal text of at most 38 digits
#: (``-?[1-9][0-9]{0,37}|0``). The first line is :func:`amount_to_text` on every write.
#: SQLite's ``GLOB`` and ``length`` stop at the first NUL character of a text, so the last
#: clause refuses any text that contains one (``instr`` compares the stored bytes and works the
#: same in UTF-8 and UTF-16 database files).
AMOUNT_CHECK = (
    "typeof(amount) = 'text' AND amount NOT GLOB '*[^0-9-]*' "
    "AND (amount = '0' OR amount GLOB '[1-9]*' OR amount GLOB '-[1-9]*') "
    "AND substr(amount, 2) NOT GLOB '*-*' AND length(ltrim(amount, '-')) <= 38 "
    "AND instr(amount, char(0)) = 0"
)

#: The entries table of schema version 1, under a given name (the migration builds it under a
#: temporary name first). Amounts are canonical decimal text: SQL never adds, compares or
#: orders them, and a value above 2**63 - 1 is stored exactly.
ENTRIES_TABLE_SQL = """CREATE TABLE {name} (
    entry_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    amount TEXT NOT NULL CHECK ({check}),
    currency TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id),
    FOREIGN KEY (account_id) REFERENCES accounts(account_id)
)"""

ENTRIES_INDEX_SQL = (
    "CREATE INDEX idx_entries_account_id ON entries(account_id)",
    "CREATE INDEX idx_entries_transaction_id ON entries(transaction_id)",
)

# Executed one by one inside one transaction (``executescript`` would commit on its own).
_SCHEMA_V1 = (
    """CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    label TEXT NOT NULL,
    regulatory_tags TEXT NOT NULL,  -- JSON array
    created_at TEXT NOT NULL        -- canonical UTC instant text
)""",
    "CREATE INDEX idx_accounts_owner_type ON accounts(owner_type)",
    "CREATE INDEX idx_accounts_currency ON accounts(currency)",
    """CREATE TABLE transactions (
    transaction_id TEXT PRIMARY KEY,
    purpose_code TEXT NOT NULL,
    initiator_id TEXT NOT NULL,
    initiated_at TEXT NOT NULL,     -- canonical UTC instant text
    settled_at TEXT,
    status TEXT NOT NULL,
    external_refs TEXT NOT NULL,    -- JSON array of [provider, ref] pairs
    metadata TEXT NOT NULL,         -- JSON array of [key, value] pairs
    compliance_decision TEXT        -- JSON object or NULL
)""",
    "CREATE INDEX idx_transactions_status ON transactions(status)",
    "CREATE INDEX idx_transactions_initiated_at ON transactions(initiated_at)",
    "CREATE INDEX idx_transactions_settled_at ON transactions(settled_at)",
    ENTRIES_TABLE_SQL.format(name="entries", check=AMOUNT_CHECK),
    *ENTRIES_INDEX_SQL,
)

#: Tables of the ledger schema. A file of version 0 that has any of them was written by 0.1.x.
LEDGER_TABLES = ("accounts", "transactions", "entries")


class SchemaVersionError(IntegrityError):
    """The file has a schema version this library does not open as it is.

    Version 0 is the format of 0.1.x: it is converted only by an explicit call of
    ``openreserve.core.migration.migrate_ledger_database``, never on open. A version higher
    than :data:`SCHEMA_VERSION` was written by a newer library.
    """


class SQLiteLedgerStorage:
    """SQLiteベースの元帳ストレージ。

    Append-only制約はアプリケーション層で強制する（save_transactionは既存IDで失敗する）。
    トランザクションのステータス更新のみ update_transaction_status() で許可するが、
    エントリーは絶対に変更しない。

    Schema version 1 (``PRAGMA user_version``): amounts are canonical decimal text, bounded
    by 10**38 - 1, and timestamps are canonical UTC text. A new or empty file is created at
    version 1. A file of 0.1.x (version 0) is refused with :class:`SchemaVersionError` and is
    converted only by an explicit call of
    ``openreserve.core.migration.migrate_ledger_database``; a file of a newer version is
    refused as well.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._open_schema()
        except BaseException:
            self._conn.close()
            raise

    def _open_schema(self) -> None:
        """Create schema version 1 in a new or empty file; accept a version 1 file; refuse
        anything else with :class:`SchemaVersionError`. Nothing is migrated here."""
        version = _user_version(self._conn)
        if version == SCHEMA_VERSION:
            _require_v1_entries(self._conn, self._db_path)
            return
        if version != 0:
            raise SchemaVersionError(
                f"ledger file {self._db_path!r} has schema version {version}; this library "
                f"reads version {SCHEMA_VERSION} only (the file was written by a newer version)"
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            version = _user_version(self._conn)  # read again under the write lock
            present = _present_tables(self._conn)
            if version != 0 or present:
                raise SchemaVersionError(
                    f"ledger file {self._db_path!r} has schema version {version} and holds the "
                    f"tables {sorted(present)}: it was written by openreserve 0.1.x. Convert it "
                    "once with openreserve.core.migration.migrate_ledger_database(path, "
                    "backup_path=..., applied_at=...); it is never converted on open"
                )
            for statement in _SCHEMA_V1:
                self._conn.execute(statement)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    @property
    def schema_version(self) -> int:
        """The ``PRAGMA user_version`` of the open file (always :data:`SCHEMA_VERSION`)."""
        return _user_version(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ---------- 口座 ----------

    def save_account(self, account: Account) -> None:
        try:
            self._conn.execute(
                "INSERT INTO accounts (account_id, owner_type, currency, label, regulatory_tags, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    account.account_id,
                    account.owner_type.value,
                    account.currency.code,
                    account.label,
                    json.dumps(sorted(account.regulatory_tags)),
                    instant_to_text(account.created_at),
                ),
            )
        except sqlite3.IntegrityError as e:
            raise IntegrityError(f"Account already exists: {account.account_id}") from e

    def load_account(self, account_id: str) -> Account:
        row = self._conn.execute(
            "SELECT account_id, owner_type, currency, label, regulatory_tags, created_at "
            "FROM accounts WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"Account not found: {account_id}")
        return Account(
            account_id=row[0],
            owner_type=OwnerType(row[1]),
            currency=Currency[row[2]] if not row[2].startswith("PROGMAT") else Currency.PROGMAT_JPY,
            label=row[3],
            regulatory_tags=frozenset(json.loads(row[4])),
            created_at=_read_instant(row[5], f"accounts.created_at of {row[0]}"),
        )

    def list_accounts(self, owner_type: OwnerType | None = None) -> list[Account]:
        # Rows come back in insertion order. Ordering by creation time is done below on
        # instants: text order in SQL equals time order only for one fixed offset.
        if owner_type is None:
            rows = self._conn.execute(
                "SELECT account_id, owner_type, currency, label, regulatory_tags, created_at "
                "FROM accounts ORDER BY rowid"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT account_id, owner_type, currency, label, regulatory_tags, created_at "
                "FROM accounts WHERE owner_type = ? ORDER BY rowid",
                (owner_type.value,),
            ).fetchall()
        accounts = [
            Account(
                account_id=r[0],
                owner_type=OwnerType(r[1]),
                currency=_currency_from_code(r[2]),
                label=r[3],
                regulatory_tags=frozenset(json.loads(r[4])),
                created_at=_read_instant(r[5], f"accounts.created_at of {r[0]}"),
            )
            for r in rows
        ]
        accounts.sort(key=lambda a: a.created_at)  # stable: ties keep insertion order
        return accounts

    # ---------- トランザクション ----------

    def save_transaction(self, transaction: Transaction) -> None:
        # Naive datetimes and amounts outside 10**38 - 1 are refused before anything is written.
        initiated_text = instant_to_text(transaction.initiated_at)
        settled_text = (
            instant_to_text(transaction.settled_at) if transaction.settled_at is not None else None
        )
        amount_texts = [amount_to_text(entry.amount.cents) for entry in transaction.entries]
        # アトミックに transactions と entries を書く
        try:
            self._conn.execute("BEGIN IMMEDIATE")

            existing = self._conn.execute(
                "SELECT 1 FROM transactions WHERE transaction_id = ?",
                (transaction.transaction_id,),
            ).fetchone()
            if existing:
                raise IntegrityError(
                    f"Transaction already exists: {transaction.transaction_id}. "
                    f"Append-only ledger: use reverse() to negate."
                )

            self._conn.execute(
                "INSERT INTO transactions "
                "(transaction_id, purpose_code, initiator_id, initiated_at, settled_at, "
                " status, external_refs, metadata, compliance_decision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transaction.transaction_id,
                    transaction.purpose_code,
                    transaction.initiator_id,
                    initiated_text,
                    settled_text,
                    transaction.status.value,
                    json.dumps([list(r) for r in transaction.external_refs]),
                    json.dumps([list(m) for m in transaction.metadata]),
                    _serialize_compliance(transaction.compliance_decision),
                ),
            )

            for entry, amount_text in zip(transaction.entries, amount_texts):
                self._conn.execute(
                    "INSERT INTO entries "
                    "(entry_id, transaction_id, account_id, amount, currency, sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entry.entry_id,
                        entry.transaction_id,
                        entry.account_id,
                        amount_text,
                        entry.amount.currency.code,
                        entry.sequence,
                    ),
                )

            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def load_transaction(self, transaction_id: str) -> Transaction:
        row = self._conn.execute(
            "SELECT transaction_id, purpose_code, initiator_id, initiated_at, settled_at, "
            "       status, external_refs, metadata, compliance_decision "
            "FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise IntegrityError(f"Transaction not found: {transaction_id}")

        entry_rows = self._conn.execute(
            "SELECT entry_id, transaction_id, account_id, amount, currency, sequence "
            "FROM entries WHERE transaction_id = ? ORDER BY sequence",
            (transaction_id,),
        ).fetchall()

        entries = tuple(
            Entry(
                entry_id=er[0],
                transaction_id=er[1],
                account_id=er[2],
                amount=Money(cents=_read_amount(er[3], er[0]), currency=_currency_from_code(er[4])),
                sequence=er[5],
            )
            for er in entry_rows
        )

        return Transaction(
            transaction_id=row[0],
            entries=entries,
            purpose_code=row[1],
            initiator_id=row[2],
            initiated_at=_read_instant(row[3], f"transactions.initiated_at of {row[0]}"),
            settled_at=(
                _read_instant(row[4], f"transactions.settled_at of {row[0]}") if row[4] else None
            ),
            status=TransactionStatus(row[5]),
            external_refs=tuple(tuple(r) for r in json.loads(row[6])),
            metadata=tuple(tuple(m) for m in json.loads(row[7])),
            compliance_decision=_deserialize_compliance(row[8]),
        )

    def update_transaction_status(self, transaction: Transaction) -> None:
        """ステータスとsettled_atのみ更新可能。エントリーは不変。"""
        settled_text = (
            instant_to_text(transaction.settled_at) if transaction.settled_at is not None else None
        )
        self._conn.execute(
            "UPDATE transactions SET status = ?, settled_at = ? WHERE transaction_id = ?",
            (
                transaction.status.value,
                settled_text,
                transaction.transaction_id,
            ),
        )

    # ---------- イテレーション ----------

    def iter_entries_for_account(
        self,
        account_id: str,
        as_of: datetime,
        statuses: tuple[TransactionStatus, ...],
    ) -> Iterator[Entry]:
        """指定口座について、as_of時点までの該当ステータスのエントリーを返す。

        A SETTLED transaction counts when ``settled_at <= as_of``; a PENDING one (only when
        PENDING is among ``statuses``) when ``initiated_at <= as_of``. Both comparisons are
        made on instants in Python. SQL neither filters nor orders by time: timestamp text
        sorts in time order only when every row uses the same offset and precision.
        """
        cutoff = _as_instant(as_of, "as_of")
        wanted = {s.value for s in statuses}
        rows = self._conn.execute(
            "SELECT e.entry_id, e.transaction_id, e.account_id, e.amount, e.currency, "
            "       e.sequence, t.status, t.initiated_at, t.settled_at "
            "FROM entries e "
            "JOIN transactions t ON e.transaction_id = t.transaction_id "
            "WHERE e.account_id = ? "
            "ORDER BY t.rowid, e.sequence",
            (account_id,),
        ).fetchall()

        selected: list[tuple[datetime, Entry]] = []
        for row in rows:
            status = row[6]
            if status not in wanted:
                continue
            initiated = _read_instant(row[7], f"transactions.initiated_at of {row[1]}")
            if status == TransactionStatus.SETTLED.value:
                if row[8] is None:
                    continue  # no settlement instant: cannot be placed at or before any cut-off
                if not _read_instant(row[8], f"transactions.settled_at of {row[1]}") <= cutoff:
                    continue
            elif status == TransactionStatus.PENDING.value:
                if not initiated <= cutoff:
                    continue
            else:
                continue  # FAILED, and the never-assigned REVERSED, are not balances
            selected.append(
                (
                    initiated,
                    Entry(
                        entry_id=row[0],
                        transaction_id=row[1],
                        account_id=row[2],
                        amount=Money(cents=_read_amount(row[3], row[0]), currency=_currency_from_code(row[4])),
                        sequence=row[5],
                    ),
                )
            )
        selected.sort(key=lambda pair: pair[0])  # stable: ties keep insertion order
        return iter([entry for _initiated, entry in selected])

    def iter_all_transactions(self) -> Iterator[Transaction]:
        rows = self._conn.execute(
            "SELECT transaction_id, initiated_at FROM transactions ORDER BY rowid"
        ).fetchall()
        ordered = [
            (_read_instant(row[1], f"transactions.initiated_at of {row[0]}"), row[0])
            for row in rows
        ]
        ordered.sort(key=lambda pair: pair[0])  # by instant; stable for equal instants
        for _initiated, transaction_id in ordered:
            yield self.load_transaction(transaction_id)


# --------- ヘルパー関数 ---------


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _present_tables(conn: sqlite3.Connection) -> set[str]:
    """Which tables of the ledger schema exist in the file."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows} & set(LEDGER_TABLES)


def _require_v1_entries(conn: sqlite3.Connection, db_path: str) -> None:
    """A file that says version 1 must have the text amount column of version 1."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()]
    if "amount" not in columns or "amount_cents" in columns:
        raise SchemaVersionError(
            f"ledger file {db_path!r} says schema version {SCHEMA_VERSION} but its entries table "
            "is not the one of that version"
        )


def _read_amount(value: object, entry_id: str) -> int:
    """Parse a stored amount. Anything but canonical decimal text is an integrity error."""
    try:
        return amount_from_text(value)  # type: ignore[arg-type]
    except ExactValueError as exc:
        raise IntegrityError(f"Unreadable amount in entries.amount of {entry_id}: {exc}") from exc


def _read_instant(text: object, where: str) -> datetime:
    """Parse a stored timestamp into an aware UTC datetime.

    Accepts the canonical text and the ISO form with an offset that earlier versions wrote.
    A value without an offset, or anything else, is an integrity error: it cannot be placed
    in time.
    """
    try:
        return instant_from_stored_text(text)  # type: ignore[arg-type]
    except ExactValueError as exc:
        raise IntegrityError(f"Unreadable timestamp in {where}: {exc}") from exc


def _as_instant(value: datetime, name: str) -> datetime:
    """Return ``value`` if it is timezone-aware; a naive datetime raises NaiveDatetimeError."""
    return require_aware(value, name)


def _currency_from_code(code: str) -> Currency:
    """通貨コード文字列から Currency enum を取得する。"""
    for c in Currency:
        if c.code == code:
            return c
    raise ValueError(f"Unknown currency code: {code}")


def _serialize_compliance(decision: ComplianceDecision | None) -> str | None:
    if decision is None:
        return None
    return json.dumps({
        "decision": decision.decision,
        "risk_score": decision.risk_score,
        "triggered_rules": list(decision.triggered_rules),
        "explanation": decision.explanation,
        "rule_version": decision.rule_version,
        "decided_at": decision.decided_at.isoformat(),
        "decided_by": decision.decided_by,
    })


def _deserialize_compliance(serialized: str | None) -> ComplianceDecision | None:
    if not serialized:
        return None
    data = json.loads(serialized)
    return ComplianceDecision(
        decision=data["decision"],
        risk_score=data["risk_score"],
        triggered_rules=tuple(data["triggered_rules"]),
        explanation=data["explanation"],
        rule_version=data["rule_version"],
        decided_at=datetime.fromisoformat(data["decided_at"]),
        decided_by=data["decided_by"],
    )
