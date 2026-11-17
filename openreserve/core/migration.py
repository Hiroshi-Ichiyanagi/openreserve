"""core/migration.py — the explicit conversion of a 0.1.x ledger file to schema version 1.

Files written by 0.1.x (schema version 0, ``PRAGMA user_version`` 0) keep amounts in a SQLite
``INTEGER`` column, a signed 64-bit value, and timestamps in whatever ISO-8601 form the caller
used. Schema version 1 keeps amounts as canonical decimal text and every timestamp as the one
canonical UTC text. :class:`~openreserve.core.storage.SQLiteLedgerStorage` refuses to open a
version 0 file; this module converts one, once, when the operator asks for it:

    migrate_ledger_database(path, backup_path=..., applied_at=...)

What it does, in this order:

1. It refuses anything that is not a version 0 ledger file, and an existing ``backup_path``.
2. It writes a complete copy of the file to ``backup_path`` with SQLite's online backup.
   There is no downgrade: 0.1.x cannot read a converted file, so the copy is mandatory.
3. In one transaction, following SQLite's documented procedure for changing a table
   (foreign keys off; build the new table; copy; drop the old one; rename; recreate the
   indexes; ``PRAGMA foreign_key_check``): every old amount must have the storage class
   ``integer`` (otherwise it stops and lists the entries); each is written as text; row
   counts and the sum per account, computed in Python, must be equal before and after; every
   timestamp of the accounts and transactions tables is rewritten as canonical UTC text; a
   row in the table ``schema_migrations`` records the step with ``applied_at`` taken from the
   argument (the module reads no clock); ``user_version`` becomes 1. Any failure rolls all of
   it back, the version number included.

A stored timestamp without an offset cannot be placed in time. With the default
``naive_timestamps="refuse"`` the migration stops and lists such values;
``naive_timestamps="assume-utc"`` is the caller's explicit statement that they mean UTC.

The audit log is a separate store and is not touched: its event hashes are computed over the
timestamp text as recorded. Timestamps inside the JSON of a compliance decision are left as
they are; they are never compared.

Run it while no other process uses the file. If another connection writes to the file after
the backup was taken, the migration notices it and stops without changing anything.

The specification is docs/hypothetical/W_CHANGEOVER_DESIGN.md, section 9.1.
"""

from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING, Any

from openreserve.core.exact import (
    AMOUNT_MAX,
    ExactValueError,
    NaiveDatetimeError,
    amount_from_text,
    amount_to_text,
    canonical_json,
    instant_from_stored_text,
    instant_to_text,
)
from openreserve.core.storage import (
    AMOUNT_CHECK,
    ENTRIES_INDEX_SQL,
    ENTRIES_TABLE_SQL,
    SCHEMA_VERSION,
    SchemaVersionError,
)
from openreserve.core.types import IntegrityError

if TYPE_CHECKING:  # the module never constructs or reads a clock
    from datetime import datetime

__all__ = [
    "LEGACY_COLUMNS",
    "MigrationError",
    "NAIVE_TIMESTAMP_POLICIES",
    "migrate_ledger_database",
]

#: Accepted values of ``naive_timestamps``.
NAIVE_TIMESTAMP_POLICIES = ("refuse", "assume-utc")

#: The columns of the three tables of a version 0 file, in table order.
LEGACY_COLUMNS = {
    "accounts": ("account_id", "owner_type", "currency", "label", "regulatory_tags", "created_at"),
    "transactions": (
        "transaction_id",
        "purpose_code",
        "initiator_id",
        "initiated_at",
        "settled_at",
        "status",
        "external_refs",
        "metadata",
        "compliance_decision",
    ),
    "entries": ("entry_id", "transaction_id", "account_id", "amount_cents", "currency", "sequence"),
}

_MIGRATIONS_TABLE_SQL = (
    "CREATE TABLE schema_migrations ("
    "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, details TEXT NOT NULL)"
)
_NEW_ENTRIES = "entries_schema_v1"
_LISTED = 20  # how many offending rows an error message names


class MigrationError(IntegrityError):
    """The content of a ledger file stops the migration. Nothing was changed in the file.

    The backup written before the migration began stays at ``backup_path``, and the message
    says so.
    """


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _check_legacy_file(conn: sqlite3.Connection, path: str) -> None:
    """Refuse anything that is not a version 0 ledger file."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != 0:
        raise SchemaVersionError(
            f"{path!r} has schema version {version}; only a version 0 ledger file (0.1.x) "
            "is migrated, and only once"
        )
    for table, expected in LEGACY_COLUMNS.items():
        found = _columns(conn, table)
        if found != expected:
            raise SchemaVersionError(
                f"{path!r} is not a version 0 ledger file: table {table} has the columns "
                f"{list(found)}, expected {list(expected)}"
            )
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_migrations'").fetchone():
        raise SchemaVersionError(f"{path!r} already has a schema_migrations table")


def _listing(rows: list[tuple[Any, ...]]) -> str:
    shown = ", ".join(repr(row) for row in rows[:_LISTED])
    more = f" and {len(rows) - _LISTED} more" if len(rows) > _LISTED else ""
    return shown + more


def _canonical_instant(text: object, naive_timestamps: str) -> tuple[str | None, str | None]:
    """``(canonical text, None)`` or ``(None, reason)``."""
    if type(text) is not str:
        return None, f"not text ({type(text).__name__})"
    try:
        return instant_to_text(instant_from_stored_text(text)), None
    except NaiveDatetimeError:
        if naive_timestamps != "assume-utc":
            return None, "no UTC offset"
        try:
            return instant_to_text(instant_from_stored_text(text + "+00:00")), None
        except ExactValueError as exc:
            return None, str(exc)
    except ExactValueError as exc:
        return None, str(exc)


def _sums_by_account(rows: list[tuple[str, int]]) -> dict[str, int]:
    sums: dict[str, int] = {}
    for account_id, amount in rows:
        sums[account_id] = sums.get(account_id, 0) + amount
    return sums


def _migrate_in_transaction(conn: sqlite3.Connection, applied_text: str, naive_timestamps: str) -> dict[str, Any]:
    # Amounts: every one must be an integer as SQLite stores it.
    bad = conn.execute(
        "SELECT entry_id, typeof(amount_cents) FROM entries "
        "WHERE typeof(amount_cents) != 'integer' ORDER BY rowid"
    ).fetchall()
    if bad:
        raise MigrationError(
            f"{len(bad)} entries do not hold an integer amount (entry id, storage class): "
            f"{_listing(bad)}; the file was not changed"
        )
    old = conn.execute(
        "SELECT entry_id, transaction_id, account_id, amount_cents, currency, sequence "
        "FROM entries ORDER BY rowid"
    ).fetchall()
    out_of_range = [(row[0], row[3]) for row in old if not -AMOUNT_MAX <= row[3] <= AMOUNT_MAX]
    if out_of_range:  # cannot happen for 64-bit integers; kept so the bound is checked here too
        raise MigrationError(f"amounts outside 10**38 - 1: {_listing(out_of_range)}")
    counts_before = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in LEGACY_COLUMNS
    }
    sums_before = _sums_by_account([(row[2], row[3]) for row in old])

    # Timestamps: every one must name an instant.
    account_times = conn.execute("SELECT account_id, created_at FROM accounts ORDER BY rowid").fetchall()
    transaction_times = conn.execute(
        "SELECT transaction_id, initiated_at, settled_at FROM transactions ORDER BY rowid"
    ).fetchall()
    unreadable: list[tuple[str, str, object, str]] = []
    account_updates: list[tuple[str, str]] = []
    transaction_updates: list[tuple[str, str | None, str]] = []
    rewritten = 0
    assumed_utc = 0

    def convert(table_column: str, row_id: str, text: object) -> str | None:
        nonlocal rewritten, assumed_utc
        canonical, reason = _canonical_instant(text, naive_timestamps)
        if canonical is None:
            unreadable.append((table_column, row_id, text, str(reason)))
            return None
        if canonical != text:
            rewritten += 1
        if naive_timestamps == "assume-utc" and _canonical_instant(text, "refuse")[0] is None:
            assumed_utc += 1
        return canonical

    for account_id, created_at in account_times:
        created = convert("accounts.created_at", account_id, created_at)
        account_updates.append((created or "", account_id))
    for transaction_id, initiated_at, settled_at in transaction_times:
        initiated = convert("transactions.initiated_at", transaction_id, initiated_at)
        settled = None if settled_at is None else convert("transactions.settled_at", transaction_id, settled_at)
        transaction_updates.append((initiated or "", settled, transaction_id))
    if unreadable:
        hint = (
            "; pass naive_timestamps='assume-utc' only if values without an offset mean UTC"
            if naive_timestamps == "refuse" and any(item[3] == "no UTC offset" for item in unreadable)
            else ""
        )
        raise MigrationError(
            f"{len(unreadable)} timestamps cannot be placed in time (column, row id, value, reason): "
            f"{_listing(unreadable)}{hint}; the file was not changed"
        )

    # The new entries table, built next to the old one, then put in its place.
    conn.execute(ENTRIES_TABLE_SQL.format(name=_NEW_ENTRIES, check=AMOUNT_CHECK))
    conn.executemany(
        f"INSERT INTO {_NEW_ENTRIES} (entry_id, transaction_id, account_id, amount, currency, sequence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(e, t, a, amount_to_text(v), c, s) for e, t, a, v, c, s in old],
    )
    copied = conn.execute(f"SELECT account_id, amount FROM {_NEW_ENTRIES} ORDER BY rowid").fetchall()
    if len(copied) != len(old) or _sums_by_account([(a, amount_from_text(v)) for a, v in copied]) != sums_before:
        raise MigrationError("the copied entries do not have the same count and sums; the file was not changed")
    conn.execute("DROP TABLE entries")
    conn.execute(f"ALTER TABLE {_NEW_ENTRIES} RENAME TO entries")
    for statement in ENTRIES_INDEX_SQL:
        conn.execute(statement)

    conn.executemany("UPDATE accounts SET created_at = ? WHERE account_id = ?", account_updates)
    conn.executemany(
        "UPDATE transactions SET initiated_at = ?, settled_at = ? WHERE transaction_id = ?",
        transaction_updates,
    )

    counts_after = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in LEGACY_COLUMNS
    }
    sums_after = _sums_by_account(
        [(a, amount_from_text(v)) for a, v in conn.execute("SELECT account_id, amount FROM entries").fetchall()]
    )
    if counts_after != counts_before or sums_after != sums_before:
        raise MigrationError("row counts or sums per account changed; the file was not changed")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise MigrationError(f"foreign key check failed: {_listing(violations)}; the file was not changed")

    details = {
        "accounts": str(counts_before["accounts"]),
        "entries": str(counts_before["entries"]),
        "from_version": "0",
        "naive_timestamps": naive_timestamps,
        "timestamps_assumed_utc": str(assumed_utc),
        "timestamps_rewritten": str(rewritten),
        "to_version": str(SCHEMA_VERSION),
        "transactions": str(counts_before["transactions"]),
    }
    conn.execute(_MIGRATIONS_TABLE_SQL)
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at, details) VALUES (?, ?, ?)",
        (SCHEMA_VERSION, applied_text, canonical_json(details).decode("utf-8")),
    )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return {
        "from_version": 0,
        "to_version": SCHEMA_VERSION,
        "accounts": counts_before["accounts"],
        "transactions": counts_before["transactions"],
        "entries": counts_before["entries"],
        "timestamps_rewritten": rewritten,
        "timestamps_assumed_utc": assumed_utc,
        "naive_timestamps": naive_timestamps,
        "applied_at": applied_text,
    }


def migrate_ledger_database(
    path: str | os.PathLike[str],
    *,
    backup_path: str | os.PathLike[str],
    applied_at: "datetime",
    naive_timestamps: str = "refuse",
) -> dict[str, Any]:
    """Convert a version 0 ledger file (0.1.x) to schema version 1, once, after a backup.

    ``backup_path`` must not exist; a complete copy of the file is written there first.
    ``applied_at`` is a timezone-aware datetime recorded in ``schema_migrations`` (the
    function reads no clock). ``naive_timestamps`` is ``"refuse"`` (default) or
    ``"assume-utc"``.

    Returns what was done: ``from_version``, ``to_version``, the row counts ``accounts``,
    ``transactions`` and ``entries``, ``timestamps_rewritten``, ``timestamps_assumed_utc``,
    ``naive_timestamps``, ``applied_at`` (canonical text) and ``backup_path``.

    Raises ``FileNotFoundError`` when ``path`` is not a file, ``FileExistsError`` when
    ``backup_path`` exists, :class:`SchemaVersionError` for a file that is not a version 0
    ledger file (a second run included), :class:`MigrationError` when the content stops the
    migration, ``NaiveDatetimeError`` for a naive ``applied_at`` and ``ValueError`` for an
    unknown ``naive_timestamps``. After any of them the file is unchanged.

    The backup is written before the migration's own transaction begins, and it is not
    removed when the migration stops. After a :class:`MigrationError`, or a
    :class:`SchemaVersionError` raised by the second check under the write lock, the file at
    ``backup_path`` is a complete copy of the unchanged file, and the error message names it;
    a second run needs another ``backup_path``, or that file removed first. The errors raised
    before the backup (``FileNotFoundError``, ``FileExistsError``, the first
    :class:`SchemaVersionError`, ``NaiveDatetimeError``, ``ValueError``) leave no file at
    ``backup_path``. An error while the backup itself is written (from ``sqlite3`` or the
    operating system) can leave a file there that is not a complete copy.
    """
    if naive_timestamps not in NAIVE_TIMESTAMP_POLICIES:
        raise ValueError(f"naive_timestamps must be one of {NAIVE_TIMESTAMP_POLICIES}")
    applied_text = instant_to_text(applied_at)
    source = os.fspath(path)
    backup = os.fspath(backup_path)
    if not os.path.isfile(source):
        raise FileNotFoundError(f"no ledger file at {source!r}")
    if os.path.lexists(backup):
        raise FileExistsError(f"backup path {backup!r} exists; give a new path")

    conn = sqlite3.connect(source, isolation_level=None)
    try:
        _check_legacy_file(conn, source)
        version_seen = conn.execute("PRAGMA data_version").fetchone()[0]
        copy = sqlite3.connect(backup)
        try:
            conn.backup(copy)
        finally:
            copy.close()
        conn.execute("PRAGMA foreign_keys=OFF")  # only takes effect outside a transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA data_version").fetchone()[0] != version_seen:
                raise MigrationError(
                    "another connection changed the file after the backup was taken; "
                    "run the migration again while no other process uses the file"
                )
            _check_legacy_file(conn, source)
            result = _migrate_in_transaction(conn, applied_text, naive_timestamps)
            conn.execute("COMMIT")
        except IntegrityError as exc:  # MigrationError, or SchemaVersionError found again
            conn.execute("ROLLBACK")
            exc.args = (
                f"{exc}; the backup at {backup!r} was written before the migration began and is a "
                "complete copy of the unchanged file: run again with another backup_path, or "
                "remove that file first",
            ) + exc.args[1:]
            raise
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    result["backup_path"] = backup
    return result
