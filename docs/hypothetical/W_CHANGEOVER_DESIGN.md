# W changeover: design decisions for openreserve (hypothetical scenario)

Status: **decided 2026-11-17.** This document records the final design decisions, the exact
formats, the reasons, the sources and the tests. Section 11 says which parts belong to stage A
(the change set this document accompanies) and which are left for later; the changelog says what a
given release actually contains.

## State of implementation

This change set implements stage A as specified in sections 4 to 9. The formats are pinned by
the vector files in `tests/vectors/`, and the test suite ends with no failure and exactly one
expected failure, the legacy tree of section 5. Nothing of stage B (section 10) is implemented.
Where the text below leaves a detail open, the code does the following:

- Section 9.1. A version 0 file is refused on open as soon as it contains any of the three
  ledger tables, also when they hold no rows. A version 0 file without them (a new or empty
  file, or one that holds only an audit log) gets schema version 1. The migration also stops,
  with the file unchanged, when another connection writes to the file between the backup and
  the migration's own transaction (detected with `PRAGMA data_version`).
- Section 9.2. `reserves_self_booked.account_ids` lists the reserve accounts of the currency
  whose settled balance at the instant is not zero, sorted. A zero-balance account adds nothing
  to the total and is left out, so a reserve account opened later does not change an earlier
  summary. `covered` is `reserves >= liabilities` also when the liabilities are 0, so a negative
  reserve total with no liabilities is not covered.
- Section 9.4. Besides the listed operations, `CurrencyRegistry.from_document(document,
  expected_hash=None)` reads a registry document in its one canonical form.

A review of the implementation led to these changes of the text, each with its tests: the two
holder checks take the kind or side the holder expects (sections 6.6, 7 and 8.6); a coverage
summary has its own check, and rounding alone can turn the coverage verdict (9.2); the column
check of the amount column refuses NUL characters (9.1); a stopped migration keeps its backup
(9.1); the figures of a debtor's bundle are magnitudes and `effective_at` is the instant of the
post snapshot (8.5, 8.6); the statements about two sources pointed at one target hold only when
no other post leaf covers the gap (8.5, 8.8); and reason codes that the code used but the text
did not list are now listed (7, 8.6).

## 0. Framing and scope

- This is a **hypothetical scenario**: "what if the current monetary system ended and a single
  world currency were introduced". It is a thought experiment used to stress the library's
  design. It is **not a forecast**, not a proposal, and not an offer of any currency, token,
  deposit, settlement, redemption or financial service.
- **"W" is a fictional scenario identifier.** It is not a currency code. In tests and examples it
  appears only as the code `FICTIONAL_W`, and it is not part of the default currency list.
- openreserve stays what its README says it is: a verification core that an operator runs on its
  own ledger. It makes **no claim to satisfy any regulation**. A successful verification
  establishes only the specific integrity and arithmetic checks documented for that artifact.

Scope statements (decision D21; the same wording is proposed for the README, see section 13):

1. **Reserve model.** The coverage check compares reserve balances booked by the operator with
   the sum of positive customer balances, per currency. It is a 1:1 reserve model. It fits
   institutions that are expected to hold reserves at least equal to customer balances, such as
   e-money issuers, payment processors, custodians and the reserve side of stablecoin issuers. It
   is not a solvency test, and it does not fit institutions that lend customer deposits; for
   those the library can at most give a commitment to liabilities and a check of conversion
   arithmetic.
2. **Fresh start.** If old balances are voided and new ones are allocated per person, there is no
   rate to apply. Only the cut-off snapshot remains in scope, as a record of what existed.
   One-person-one-claim checks are out of scope.
3. **Completeness.** A verification covers the records that were committed. It does not
   establish that every account, debt or encumbrance was recorded, that reserve assets exist or
   are available, that claims are enforceable, or that an institution is solvent.
4. **Publication.** A holder's check means something only against a root that every holder sees
   identically. This library does not publish, sign or witness roots (stage B).
5. **Identifiers.** A proof binds an account identifier chosen by the operator. A holder must
   check that it is an identifier they already knew to be uniquely theirs.
6. **Who checked.** Holder checks protect only the holders who actually check. Leaves nobody
   checks are covered only by a full recomputation from the complete leaf list.

In a fixed-rate changeover (old currencies converted at declared rates on a cut-off date), an
operator-side tool is asked to show: each holder's balance at the cut-off; the institution's
totals; that the declared rate and the published rounding rule were applied to every balance,
debts included; that reserves cover the converted balances; and what was published, and when.
Stage A provides checks for the first three, within the limits stated above. Reserve evidence
from outside the ledger and publication evidence are stage B.

## 1. Measured facts

Measured on 2026-11-17 with Python 3.14.9, pytest 9.0.3 and SQLite 3.54.1, on a private copy of
the code as it was before this change set (the last commit of `main`, `4049d82`, plus the tests
file with the five pinned defects). The reference code and the test vectors were also run on
Python 3.11.17, the oldest supported version; the suite itself was not run there.

| # | What was measured | Result |
|---|---|---|
| M1 | Suite | `194 passed, 5 xfailed` (`python3 -m pytest -q`) |
| M2 | The five pinned defects | (a) leaf sets `[a,b,c]` and `[a,b,c,c]` share one root while totals are 475000 and 530000; (b) `MerkleProof.verify()` rejects a genuine proof; (c) `fx_convert` cannot be posted; (d) 10 units of an 18-decimal currency raise `OverflowError` in the SQLite store; (e) a sibling balance is recovered from a holder's proof with public inputs only |
| M3 | As-of comparison (`core/storage.py` lines 303-310 compare ISO-8601 text in SQL) | entry settled 2026-01-01T00:00Z. Cut-off 2026-01-01T08:00+09:00 (one hour before): balance 100, expected 0. Cut-off 2025-12-31T20:00-05:00 (one hour after): balance 0, expected 100. Naive cut-off at the same wall time: 0, expected 100 or a refusal. Settlement stored as `2026-01-01T09:00:00+09:00` (the same instant) and a UTC cut-off at that instant: 0, expected 100. Stored text keeps the caller's offset |
| M4 | A customer account can be overdrawn by a plain transfer | reserves 50, customer balances +100 and -60: published liabilities **40**, `is_solvent` **True**, coverage 1.25. The sum of positive balances is 100, so the reserves cover half |
| M5 | `coverage_ratio` | a `float`; with no liabilities the sentinel `999.99`, published as `9999900` basis points; `int(ratio * 10000)` differs from the exact floor in 42 of the 40000 pairs with reserves and liabilities from 1 to 200 (reserves 57, liabilities 100 gives 5699, exact 5700) |
| M6 | SQLite | binding a Python integer outside signed 64 bits raises `OverflowError` whatever the column type; a column declared `INTEGER` that is given the text `10000000000000000000` stores the REAL value `1e+19` (silent loss); a `TEXT` column keeps the text; `SUM` over text amounts returns a REAL; `ORDER BY` on text amounts is lexicographic; `PRAGMA user_version` is rolled back with its transaction |
| M7 | Regenerating a past snapshot | after a new customer account is opened later, the same `snapshot_at` gives a different root, because every customer account becomes a leaf, also with balance 0 |
| M8 | Datetimes that reach storage in the existing suite | 426 account rows, 216 posted transactions, 144 status updates, 182 as-of queries: all timezone-aware and all UTC; none naive |
| M9 | Negative customer balances in the existing suite | one test (`test_psa_g4.py`, negative balance excluded from held funds) relies on a customer account going negative, so the ledger must keep allowing it |
| M10 | Ledger state rules | `reverse()` can be called twice on one transaction (balance -100 after reversing a +100 twice); reversing a PENDING transaction posts a SETTLED reversal of entries that never counted; `settle()` accepts a `settled_at` earlier than `initiated_at`; an as-of balance with `include_pending=True` changes when a pending transaction settles later; the status `REVERSED` is never assigned anywhere |
| M11 | Floats in money paths | only `transparency/proofs.py` lines 40, 116 and 128 (the coverage ratio); no `Decimal` or `Fraction` anywhere in the package |
| M12 | Legacy code and the last tag | between tag `v0.1.1` and `main`, the modules `core`, `transparency`, `regulatory` and `providers` differ only by the added file `transparency/audit_query.py` |
| M13 | Guard tests | `tests/test_superrich_guards.py` and `tests/test_determinism_guard.py` replace the name `datetime` inside `core/ledger.py`, `core/storage.py`, `transparency/audit_log.py` and `transparency/proofs.py` with a stand-in that has only `now` (raising), `fromisoformat`, and in one test `max` and `min` |
| M14 | Trial changes on a private copy | with the verifier fix, instant comparison, canonical UTC text, aware-only datetimes, negative balances left out of the legacy tree, text amounts, the new `fx_convert` signature, an explicit reversal time and an exact coverage ratio: all 194 existing tests still pass; markers (b) and (d) flip to unexpected passes; (c) fails only because the test still uses the old call; (a) and (e) stay expected failures until the new tree is used. All seven example scripts still run. The legacy root for a ledger with fixed account ids alice, bob, carol is `746446bf...215c` before and after |
| M15 | Storage migration trial | on a file written by the unmodified code: 8 entries and 10 timestamps rewritten; a balance that the unmodified code reported as 0 (settlement stored with `+09:00`) is 9223372036854775807 afterwards; 10 units of the 18-decimal currency post on the migrated file and the balance is exact above 2^63; a second migration is refused; the unmodified code cannot read the migrated file (`no such column`) |
| M16 | Ledger-level conversion trial | six accounts with a balance converted (one more had none) through one clearing account per currency: each currency journal balances; the source clearing account ends at 0; the target clearing account holds +1 before closing and 0 after; the closing entry books 1 minor unit to the residue account; rebuilding the evidence from the same ledger gives the same manifest hash |
| M17 | Reference tree timing (pure Python) | 1,000,000 leaves: 4.6 s for salts, leaf hashes and sorting, 0.85 s to build, 21 microseconds to verify one proof of 20 steps |
| M18 | Reference verifiers under mutation | 33,500 mutated proofs, manifests, bundles and exports: no exception, and no altered document accepted. Two holes found this way were closed in the specification (exact key sets everywhere; the whole document must fit the canonical value profile before any other check) |
| M19 | Snapshot rules on the ledger | the rules of section 7, run against the trial copy of the ledger with the reference tree, give the pinned values: summary hash `c981cf21...d4ed` for the fixed-id ledger (also with accounts created in another order, a pending transfer and a zero-balance account present; also for the cut-off given as `+09:00`; also when regenerated after a later account and later transfers), coverage summary hash `a15cf45f...b678`, and for the overdraft ledger liabilities 100, receivables 60 and a negative verdict |

Not measured: the suite on Python 3.11 to 3.13 (the CI matrix); generation time on a large
ledger (one balance query per account); whether the package on the package index equals the tag
`v0.1.1`; any external system.

## 2. Sources

All read on 2026-11-17 unless a different date is given. Content is paraphrased; the few literal
fragments are short and marked with quotation marks.

**Read in full or in the relevant part**

| # | Source | What it supports here |
|---|---|---|
| S1 | Council Regulation (EC) No 1103/97 of 17 June 1997, EUR-Lex, original text <https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:31997R1103> and consolidated text of 2001-01-01 <https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:01997R1103-20010101> (Articles 4 and 5 are identical in both) | Article 4(1): the rate is adopted as one unit of the new currency expressed in each old currency, with six significant figures. Article 4(2): the rates are not rounded or truncated when converting. Article 4(3): the same rates are used both ways and "Inverse rates derived from the conversion rates shall not be used". Article 4(4): conversion between two old currencies goes through the new unit, rounded to not less than three decimals. Article 5: results are rounded to the nearest sub-unit, and a result that is "exactly half-way" is rounded up. Recital 10: inverse rates would imply rounded rates and inaccuracies, notably for large amounts. Recital 11: the rounding rules do not affect practices that give more accuracy in intermediate computations. Recital 12: significant figures are counted from the left, starting at the first non-zero figure. Article 2(1): a basket unit was replaced by the new currency at one to one. **The text says nothing about negative amounts.** |
| S2 | Y. Ji and K. Chalkias, "Generalized Proof of Liabilities", ACM CCS 2021, author's copy <https://www.yji.me/publication/gpol/gpol.pdf> | The original summation tree hashes the node's own sum with the two child hashes; a prover can then set a node's value to the larger child instead of the sum and still serve proofs that verify (their appendix shows four users and a root of 4 instead of 10). The repaired construction hashes both child values and both child hashes, `h = H(v_l || v_r || h_l || h_r)`, and they attribute this repair to Hu, Zhang and Guo. Each holder sees the amount of the leaf next to theirs, and the position reveals the population. Sums must be kept in a range that cannot overflow |
| S3 | K. Chalkias, P. Chatzigiannis and Y. Ji, "Broken Proofs of Solvency in Blockchain Custodial Wallets and Exchanges" (ePrint 2022/043; FC 2022 workshop), author's copy <https://www.yji.me/publication/pol-attacks/pol-attacks.pdf> | The same repair, again attributed to Hu, Zhang and Guo. Truncated hashes make collisions practical (their example: 64-bit truncation). If the prover alone chooses identifiers, two holders with equal balances can be shown the same leaf. A root must be published where every holder sees the same one, and only one per round. Proofs disclose balances of other holders unless amounts are split or hidden. A prover who learns which holders never check can leave them out later |
| S4 | RFC 6962, section 2.1 <https://www.rfc-editor.org/rfc/rfc6962.txt> | Tree shape for any number of leaves: split at the largest power of two smaller than n; no padding and no duplication; the shape is determined by the number of leaves. Leaf and node hashes use different prefixes (0x00 and 0x01) for second-preimage resistance |
| S5 | RFC 9162, section 2.1.3.2 <https://www.rfc-editor.org/rfc/rfc9162.txt> | The inclusion check for that shape from a leaf index, a tree size and a path |
| S6 | Bitcoin Core, `src/consensus/merkle.cpp`, header comment <https://raw.githubusercontent.com/bitcoin/bitcoin/master/src/consensus/merkle.cpp> | Duplicating the last node of an odd level lets different leaf lists share one root (CVE-2012-2459). This is the ambiguity of defect (a) |
| S7 | RFC 8259 section 6 and RFC 7493 section 2.2 <https://www.rfc-editor.org/rfc/rfc8259.txt>, <https://www.rfc-editor.org/rfc/rfc7493.txt> | JSON numbers are exact across implementations only within plus or minus (2^53 - 1); for larger exact integers, encoding them as strings is recommended |
| S8 | RFC 8785, sections 3.2.2.2 and 3.2.3 <https://www.rfc-editor.org/rfc/rfc8785.txt> | Canonical JSON: string escaping rules and sorting of member names by UTF-16 code units |
| S9 | RFC 2104, section 3 <https://www.rfc-editor.org/rfc/rfc2104.txt> | HMAC keys shorter than the hash output are strongly discouraged |
| S10 | RFC 3339, section 5.1 <https://www.rfc-editor.org/rfc/rfc3339.txt> | Timestamp strings sort in time order only if all use the same zone, the same zone notation and the same number of fractional digits |
| S11 | Python documentation, `datetime` <https://docs.python.org/3/library/datetime.html> | Aware datetimes compare as instants; ordering a naive against an aware datetime raises `TypeError`; `fromisoformat` accepts more ISO 8601 forms since 3.11 |
| S12 | SQLite documentation: datatypes <https://www.sqlite.org/datatype3.html>, table changes <https://www.sqlite.org/lang_altertable.html>, pragmas <https://www.sqlite.org/pragma.html>; core functions <https://www.sqlite.org/lang_corefunc.html> and NUL characters in strings <https://www.sqlite.org/nulinstr.html> (both read on 2026-11-17) | INTEGER is a signed integer of at most 8 bytes; well-formed integer text too large for 64 bits put into a column with INTEGER or NUMERIC affinity becomes REAL; a TEXT column stores text; the documented way to change a table is to build a new table, copy, drop and rename inside one transaction with foreign keys off, then run `foreign_key_check`; `user_version` is an integer reserved for the application. A TEXT value can hold NUL characters; `length` counts only up to the first one, while `instr(X, char(0))` finds an embedded NUL |
| S13 | M. Mohamed and G. Karame, "Mitigating Collusion in Proofs of Liabilities", arXiv:2603.12990 <https://arxiv.org/abs/2603.12990> (section 2.2 only) | A recent summary of the summation-tree design: holders check inclusion, there is no auditor role, and each proof leaks partial information about other balances |

**Not read, and how the design avoids depending on it**

| Source | Status | Consequence |
|---|---|---|
| K. Hu, Z. Zhang and K. Guo, "Breaking the Binding: Attacks on the Merkle Approach to Prove Liabilities and its Applications", IACR ePrint 2018/1139 <https://eprint.iacr.org/2018/1139> | The abstract page was read (it states the attack and that two repairs are proposed, one of them proved). The PDF returned HTTP 403 on 2026-11-15 and again on 2026-11-17 (a browser check page). The journal version was not tried | The node construction is taken from S2 and S3, which both state it and attribute it to this paper, and section 6.6 gives a self-contained argument for the property this library relies on. Nothing depends on a detail found only in the unread paper |
| ISO 4217 (currency code syntax) | The ISO page returned HTTP 403 | The rule for fictional codes (section 9.4) is purely syntactic and defined here; it does not rely on ISO's syntax |
| European Commission page "Converting to the euro" | Read in summary on 2026-11-15 while the questions were collected, not re-read | Superseded by S1, the regulation it summarises |

## 3. Decision summary

Items R1 to R10 and D1 to D23 are the numbered design questions this document answers. E1 to E4
are decisions found necessary while answering them. "A-core" is the calculation and proof core of
stage A, "A-rest" the rest of stage A, "B" is later.

| Id | Decision in one line | Section | Stage |
|---|---|---|---|
| D1 | SHA-256, untruncated. A node hashes both child hashes and both child sums; sums are 16-byte big-endian integers bounded by 10^38 - 1. No level or position inside node hashes; a separate root hash binds context, leaf count and total | 6.1 | A-core |
| D2 | No padding and no duplication: the tree shape of RFC 6962 for any leaf count. The leaf count is authenticated inside the root hash. The empty tree is a typed special case | 6.2 | A-core |
| D3 | A leaf hashes a tree context (kind, institution, currency, minor units, instant, binding), a slot id, the amount, a 32-byte salt and a payload, each variable part length-prefixed. A slot is one ledger account | 6.1 | A-core |
| D4 | Positive customer balances go to a liabilities tree, negative ones to a separate receivables tree, zero balances to neither. Never netted. Overdrafts are not forbidden. The legacy generator stops netting too | 7 | A-core |
| D5 | Salts are derived, not stored: HMAC-SHA256 under an operator key of at least 32 bytes that is an explicit input. A holder gets their salt inside their proof; an auditor gets all salts in the export; a public summary has none | 6.3 | A-core (key handling guidance and balance splitting: B) |
| D6 | The disclosure is documented exactly and pinned by a test: a proof shows the sum of every sibling subtree, so usually one other leaf's exact amount, never an identifier. Leaves are ordered by salted leaf hash so the neighbour is not predictable. Splitting balances is stage B | 6.7 | A-core |
| D7 | The legacy tree stays byte-identical and keeps serving the intent and execution commitments. The reserve artifact gets the new tree under new names. `verify_proof_with_node_prefix` stays as another name for the one legacy verifier | 5 | A-core |
| D8 | `MerkleProof.verify()` is fixed now, on the legacy encoding, with an optional expected root. New verifiers take the expected root as a required argument, return a result object, and never raise on the content of a proof | 5, 6.4 | A-core |
| D9 | New public names avoid "solvency": `ReserveCoverage`, `DeclaredReserveCoverage`, `is_covered`, `all_covered`. Old names keep working during a deprecation period. README wording stays a proposal | 9.3 | A-rest |
| D10 | Conversion evidence is a hash-identified manifest in a strict canonical JSON profile, a holder bundle of three inclusion proofs linked through a tree of conversion records, and an auditor export. No signature in stage A; signatures are detached and come later | 8.5 | A-core |
| D11 | A rate is decimal text turned into an exact fraction. The quotation is declared: `SOURCE_PER_TARGET` (the direction of the euro precedent, converted by exact division) or `TARGET_PER_SOURCE`. The library never inverts a rate. A declared number of significant figures is checked | 8.1, 8.2 | A-core |
| D12 | The tie rule is part of the declaration: `HALF_AWAY_FROM_ZERO` or `HALF_EVEN`, both symmetric in sign. Rounding happens once per account, to one minor unit of the target currency | 8.2 | A-core |
| D13 | The whole-ledger rounding difference is booked from the target clearing account to a residue account of the existing type `FX_GAIN_LOSS`; the institution bears it. Per-side reconciliation figures and exact fractional residues are in the manifest as `num`/`den` strings | 8.4 | A-core |
| D14 | One clearing account per currency for each declaration. One transaction per converted account carries both legs; its id is derived from the declaration and the source account, so a replay is refused by the store | 8.3 | A-core |
| D15 | A minimal registry: code, minor units, fictional flag; canonical JSON; SHA-256 identity; the default equals today's eight currencies; fictional codes start with `FICTIONAL_`. Effective dates and successor entries: B | 9.4 | A-rest |
| D16 | Amounts are canonical decimal text, in SQLite `TEXT` and in JSON strings, bounded by 10^38 - 1. Existing files are migrated by an explicit function that makes a backup first | 4.1, 9.1 | codec A-core, storage A-rest |
| D17 | Coverage is the exact pair of totals plus `floor(10000 * reserves / liabilities)`; `null` when there are no liabilities; no sentinel; a `Fraction` in Python | 9.2 | A-rest |
| D18 | Reserve evidence from outside the ledger: three statuses that say only what was checked; no public-key check in the core | 10 | B |
| D19 | Publication manifests chained by hash, detached signatures checked by optional plug-ins, an evidence format for conflicting roots, no network code in the core | 10 | B |
| D20 | Datetimes must be timezone-aware; they are stored as one canonical UTC text; every comparison and ordering is done on instants in Python, never on text in SQL; `reverse()` gets an explicit time | 4.3 | A-core |
| D21 | The six scope statements of section 0 | 0, 13 | wording decided; README change is a proposal |
| D22 | Leaf order is by leaf hash, never by creation time; conversion transaction ids are derived from content. Content-addressed ids for exports: B | 6.2, 8.3, 10 | A / B |
| D23 | Jurisdiction rules move under a jurisdiction package with a rule-set id and revision; old imports keep working for a deprecation period | 10 | B |
| E1 | A snapshot contains exactly the entries of transactions settled at or before the instant. Pending transactions, holds, accrued interest and later corrections are handled by stated rules | 4.3 | A-core |
| E2 | Every new artifact carries a `format` string; artifacts without one are the legacy format, which stays verifiable. The first release with these changes is 0.2.0 | 9.5 | A-rest |
| E3 | The design document lives in the repository only in this public-safe form | 9.6 | A-rest |
| E4 | The two measured defects without a test (non-UTC cut-off, negative leaf) get strict expected-failure tests first, then the fix | 12 | A-core |

## 4. Foundations

### 4.1 Exact integers (R4, the codec part of D16)

**Decision.** An amount is a Python `int` in minor units, never a `float`, `Decimal`, `bool` or
string inside the library. One bound holds everywhere: `AMOUNT_MAX = 10**38 - 1`. It applies to
the magnitude of a stored entry, to leaf amounts, to every sum and total in a tree, and to
converted amounts. A value outside the bound is refused; nothing wraps around and nothing is
clamped.

Why 10^38 - 1: it leaves room for 10^20 whole units of an 18-decimal currency; every value fits
a signed 128-bit integer; and two in-range values add up to less than 2^128, so a verifier
written with unsigned 128-bit arithmetic cannot overflow in one addition before it checks the
range. It is also the 38-digit precision that several SQL decimal types use (general knowledge,
not checked against a source; the decision does not rest on it).

Alternatives. 2^63 - 1: measured too small (M2 d). No bound: a fixed-width encoding inside node
hashes needs one, and a verifier in another language needs to know the range. 2^128 - 1: the
encoding would have no headroom for one unchecked addition. 2^256 - 1: more room than any
realistic total needs, and no native type in most languages.

**Three encodings, one value.**

| Where | Encoding | Rule |
|---|---|---|
| Text (SQLite, JSON, hashing of documents) | canonical decimal | must match `-?[1-9][0-9]{0,37}\|0` as a **full match**; so no `+`, no leading zero, no `-0`, no spaces, no line break at the end, no digit separators, ASCII digits only. Unsigned fields use `[1-9][0-9]{0,37}\|0` |
| Bytes inside tree hashes | 16 bytes, unsigned, big-endian | only for values from 0 to `AMOUNT_MAX` |
| Python | `int` | `type(value) is int` (so `bool` is refused) |

JSON: in every new format, **every integer is a JSON string** in canonical decimal form, counts
and indexes included. The new formats contain no JSON number at all (S7). The legacy summaries
keep their JSON numbers; they are exact only up to 2^53 - 1, and that limit is now documented.

SQL never does arithmetic, comparison or ordering on amounts (M6). Sums are computed in Python.

Functions (module `openreserve/core/exact.py`; the names are part of the decision because the
tests import them):

```
AMOUNT_MAX = 10**38 - 1
class ExactValueError(ValueError)
amount_to_text(value: int) -> str                       # refuses non-int, bool, out of range
amount_from_text(text: str, *, signed: bool = True) -> int   # refuses non-str and non-canonical text
amount_to_u128(value: int) -> bytes                     # 0 <= value <= AMOUNT_MAX
```

`Money` additionally refuses `bool` for `cents` (today `Money(cents=True)` is accepted).

Tests: `tests/vectors/exact_v1.json` lists 10 valid texts (among them 9223372036854775808,
10^30 and plus or minus (10^38 - 1)), 22 invalid texts (among them `-0`, `01`, `1.0`, `1e3`,
`" 1"`, `"1\n"`, `1_000`, full-width digits, 10^38) and 7 byte encodings. Every valid text must
round-trip; every invalid one must raise `ExactValueError`; the encoder must refuse `True`,
`1.0`, `"1"`, `None`, `10**38` and `-(10**38)`.

### 4.2 Canonical JSON (the canonicalization part of D10 and D19)

**Decision.** One strict profile is used for every document that is hashed (declarations,
manifests, registries, record payloads) and is the recommended form for every other new artifact:

- values are objects, arrays, strings, `true`, `false` and `null`; **numbers are not allowed**;
- object keys match `[a-z][a-z0-9_]*`;
- output is UTF-8 with no insignificant whitespace, keys sorted by code point, strings escaped as
  in RFC 8785 section 3.2.2.2 (only `"`, `\` and control characters are escaped; U+0008, U+0009,
  U+000A, U+000C, U+000D as `\b \t \n \f \r`; other control characters as `\u00xx` in lower
  case), and a string with a lone surrogate is refused.

For such documents the output equals RFC 8785 (S8): there are no numbers, and for ASCII keys
code-point order equals UTF-16 code-unit order. In Python it is
`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`
after the type check; this was compared with the RFC rule for every code point up to U+024F and
a few beyond (no difference). The hash of a document is the SHA-256 of these bytes, in lower-case
hexadecimal.

```
canonical_json(obj) -> bytes      # raises ExactValueError for int, float, bad keys, other types
sha256_hex(data: bytes) -> str
```

Alternative: full RFC 8785 with numbers. Rejected: number serialization is the hard part of that
RFC, and this library has no use for JSON numbers.

Tests: six documents with their exact bytes and hashes in `exact_v1.json`, for example
`{"b":"2","a":"1"}` gives the bytes `{"a":"1","b":"2"}` and the hash
`21f76dfbfe6dfe21f762080ef484112cf2952974cef30741fd1931e1c6d92112`; an `int`, a `float`, a key
`"A"` and a non-string key are refused.

### 4.3 Instants and the cut-off (D20, E1)

**Problem.** M3: the as-of comparison is a comparison of text, which is valid only when every
string uses the same zone, the same zone notation and the same number of fractional digits (S10).
The stored text keeps whatever offset the caller used, so a cut-off given in another offset, a
settlement recorded in another offset, or a naive datetime gives a wrong balance in either
direction.

**Decision.**

1. Every datetime that enters the ledger, the store or a generator must be timezone-aware. A
   naive datetime raises `NaiveDatetimeError` (a subclass of `ExactValueError`). The existing
   suite passes only aware UTC values (M8), so no existing test changes.
2. A datetime is converted to UTC first and only then compared (S11). Text is never compared.
3. One canonical text for an instant, used in the store and in every new artifact:
   `YYYY-MM-DDTHH:MM:SS.ffffffZ`: UTC, exactly six fractional digits, the letter `Z`, 27
   characters. It is built from the fields (not with `strftime`, whose year padding differs
   between platforms) and parsed by a strict pattern. Reading the store also accepts the older
   ISO forms with an explicit offset; a stored value without an offset raises `IntegrityError`
   (the migration of section 9.1 offers an explicit way to resolve those).
4. The store no longer filters or orders by time in SQL. Queries return rows in insertion order
   and Python filters and sorts on instants: a settled entry counts when
   `settled_at <= as_of`, a pending one (only when asked for) when `initiated_at <= as_of`. The
   clause for the never-assigned status `REVERSED` is removed.
5. `Ledger.reverse()` gets the keyword `reversed_at`; when given, the reversal is initiated and
   settled at that instant. The default stays the wall clock, as for the other ledger
   operations.

```
class NaiveDatetimeError(ExactValueError)
require_aware(value: datetime, name: str = "datetime") -> datetime
instant_to_text(value: datetime) -> str
instant_from_text(text: str) -> datetime            # strict canonical form only
instant_from_stored_text(text: str) -> datetime     # canonical or legacy ISO with offset; naive refused
```

Implementation constraint (M13): the guard tests replace the name `datetime` inside
`core/storage.py`, `core/ledger.py` and `transparency/proofs.py`. Those modules must therefore
call the helpers of `core/exact.py` and must not themselves construct datetimes or test
`isinstance(x, datetime)`.

Alternatives. Refuse every non-UTC datetime: needlessly strict, since conversion is exact.
Normalise on write but keep comparing text in SQL: still wrong for rows written by older
versions. Store integers (microseconds since an epoch): a larger change of the file format for
no gain once comparison happens in Python.

**Cut-off rules (E1).** A snapshot at instant T:

- contains exactly the entries of transactions whose status is SETTLED and whose `settled_at`
  is at or before T (inclusive, compared as instants). The summary names the rule:
  `"inclusion_rule": "SETTLED_AT_OR_BEFORE_SNAPSHOT"`;
- never contains a PENDING transaction, whatever its start time. A conversion refuses an account
  that still has a pending transaction (section 8.3): the operator settles or fails it first;
- holds: the ledger has no hold concept. Funds on hold are still owed to the customer; they stay
  in the liabilities tree only if they stay in a customer account. A hold booked to another
  account type leaves the tree, and the library cannot see that;
- accrued interest counts only once the operator has posted and settled it at or before T;
- corrections after the cut-off are new transactions settled after T (for example a reversal
  with an explicit time). They do not change the snapshot at T. Nothing in stage A stops an
  operator from posting a backdated settlement; if that happens, regenerating the snapshot gives
  a different root than the published one, which shows the change. A lock on closed periods is
  stage B.

Other wall-clock defaults were reviewed and stay as they are in stage A, because removing them
changes many call sites: the start time of a transaction builder, `settle()`, `balance()`,
`all_balances_snapshot()`, `aggregate_by()`, the creation time of an account, and
`AuditLog.append()`. The generators of proofs and snapshots already require an explicit time.
Account creation time is not used to decide what a snapshot contains (section 7).

Tests (E4 first adds the failing case as a strict expected failure):

- settled 2026-01-01T00:00Z: balance at `2026-01-01T08:00+09:00` is 0, at
  `2025-12-31T20:00-05:00` is 100, and the same two instants given in UTC give the same answers;
- a settlement passed as `2026-01-01T09:00+09:00` is counted at the cut-off 2026-01-01T00:00Z,
  and the stored text is `2026-01-01T00:00:00.000000Z`;
- a naive cut-off, a naive settlement time and a naive start time each raise
  `NaiveDatetimeError`;
- the six instant texts of `exact_v1.json` (for example `2026-06-30T23:59:59.999999+05:45`
  becomes `2026-06-30T18:14:59.999999Z`, and the year 99 becomes `0099-01-01T00:00:00.000000Z`)
  and its 12 invalid texts;
- a row written in the older form (`2026-01-01T00:00:00+00:00`) is still read and compared
  correctly; a row without an offset raises `IntegrityError` on read;
- `reverse(..., reversed_at=T)` twice on two equal ledgers gives equal reversal times.

## 5. One correct verifier (R9, D7, D8)

**Problem.** `MerkleProof.verify()` hashes pairs without the `NODE:` prefix that the tree uses,
so it rejects every genuine proof with at least one step (M2 b). Only the separate function
`verify_proof_with_node_prefix()` works, and a proof carries its own root, so a caller who forgets
to compare that root with the published one has checked nothing.

**Decision.**

1. The legacy tree (`transparency/merkle.py`: leaf `SHA-256("LEAF:<id>:<balance>:<currency>")`,
   node `SHA-256("NODE:" + left + right)`, an odd last node paired with itself) is **frozen**:
   its bytes do not change. It keeps serving the commitments of the intent and execution modules,
   whose roots therefore do not change, and it keeps verifying artifacts published by 0.1.x.
2. There is exactly one legacy verification routine. `MerkleProof.verify(expected_root=None)`
   and `verify_proof_with_node_prefix(proof)` both call it. The helper `hash_pair()`, whose
   missing prefix caused the defect, is removed.
3. `verify(expected_root=...)` compares the recomputed root with the given root. Without the
   argument it compares with the root stored in the proof, which only shows that the proof is
   consistent with itself; the documentation says so.
4. Malformed input gives `False`, not an exception: a sibling hash or root that is not exactly 64
   lower-case hexadecimal characters, an `is_left` that is not a `bool`, or more than 64 steps.
5. The reserve artifact stops using the legacy tree for new work: the new tree of section 6 is
   used under new names (section 9.3). The legacy generator stays callable during the
   deprecation period.
6. The known weakness of the legacy tree stays visible: a strict expected-failure test keeps
   asserting that two leaf lists with different totals must not share a root. It is fixed when
   the intent and execution commitments move to a count-bound tree (stage B).

Alternatives. Fix `verify()` only in the new tree: leaves a public method that is wrong. Move
the intent and execution commitments to the new tree now: changes the roots of four more
artifacts and their documents; out of proportion for stage A. Remove
`verify_proof_with_node_prefix`: the current guide and examples use it.

**Tests.**

- Test (b) passes as written and its marker is removed.
- `tests/vectors/legacy_merkle_v0.json` (computed without the library, then compared with it):
  six trees with every proof. Roots: one leaf `0654bedb...dd4b`; two `de6dcbd7...37cd`;
  alice, bob, carol `746446bf3f90b11cf1311d7b2e3193c81c3cb0d4dce4ef8416c85ae62cdf215c`; the same
  root for alice, bob, carol, carol (the frozen ambiguity); five mixed values including a
  negative, zero, 2^63 and 10^30 `412b3558...bb95`; the empty placeholder `62d32fac...2e8b`. The
  library must reproduce every root and accept every proof with both entry points.
- For trees of 1 to 33 leaves, `verify()`, `verify(expected_root=root)` and
  `verify_proof_with_node_prefix()` agree on every genuine proof.
- Each of these gives `False`: a flipped `is_left`; a replaced sibling; a dropped step; an added
  step; a wrong expected root; the leaf of another account; a sibling with a non-hexadecimal
  character, with upper-case letters, with 63 or 65 characters, or with a space inside; 65 steps.
- A proof whose own `root_hash` was replaced verifies with `verify()` only if the replacement is
  the true root, and `verify(expected_root=published)` is `False` for any other published value.
- A ledger with fixed account ids alice, bob, carol (300000, 120000, 55000, created in that
  order) still gives the legacy root `746446bf...215c` (M14).

## 6. The Merkle-sum tree, format `openreserve.sumtree/1` (R1: D1, D2, D3, D5, D6)

**Problem.** In the legacy tree a node hashes only its two child hashes, the total is computed
outside the tree and published next to the root, and an odd last node is paired with itself. So
no holder's proof can notice an understated or inflated total, and two leaf lists with different
totals share a root (M2 a). Leaves have no secret, so a sibling hash lets anyone who knows an
account id test guesses of its balance (M2 e).

**Decision in short.** A new tree, next to the frozen legacy tree. Every node carries a sum.
A node hash binds both child hashes and both child sums (S2, S3). There is no padding; the shape
is that of RFC 6962 for any number of leaves (S4). A final root hash binds the tree context, the
leaf count and the total, so the one published value answers "which tree, how many leaves, what
total". Leaves are salted, and ordered by their hash.

### 6.1 Byte layouts

```
H         = SHA-256, full 32-byte output (never truncated)
DOMAIN    = the 22 ASCII bytes  openreserve.sumtree.v1
u8(x)     = 1 byte
u32(x)    = 4 bytes, big-endian
u64(x)    = 8 bytes, big-endian
u128(x)   = 16 bytes, big-endian, only for 0 <= x <= 10**38 - 1
lp(b)     = u32(len(b)) || b
utf8(s)   = the UTF-8 bytes of s, taken as they are (no normalisation)

context   = H( DOMAIN || 0x03 || lp(utf8(kind)) || lp(utf8(institution_id)) || lp(utf8(currency))
               || u8(minor_units) || lp(utf8(as_of)) || lp(binding) )
salt      = HMAC-SHA256( key = salt_key,  message = DOMAIN || 0x10 || context || lp(utf8(slot_id)) )
leaf      = H( DOMAIN || 0x00 || context || lp(utf8(slot_id)) || u128(amount) || salt || lp(utf8(payload)) )
node      = H( DOMAIN || 0x01 || left_hash || u128(left_sum) || right_hash || u128(right_sum) )
            node_sum = left_sum + right_sum          (an exact integer; refused above 10**38 - 1)
root      = H( DOMAIN || 0x02 || context || u64(leaf_count) || u128(total) || top_hash )
```

Every hash input starts with the same 22 bytes followed by one type byte (0x00 leaf, 0x01 node,
0x02 root, 0x03 context), so an input of one type can never be read as another. A node input is
always 119 bytes and a root input 111 bytes; the variable parts of a context and of a leaf are
length-prefixed, so two different field lists never give the same bytes.

Fields:

| Field | Rule |
|---|---|
| `kind` | one of `liabilities`, `receivables`, `conversion-liabilities`, `conversion-receivables` |
| `institution_id` | a string chosen by the operator; 1 to 255 bytes in UTF-8; no character below U+0020 and no U+007F |
| `currency` | a code matching `[A-Z][A-Z0-9_]{0,31}` |
| `minor_units` | 0 to 30 |
| `as_of` | the canonical instant text of section 4.3 (27 ASCII bytes) |
| `binding` | empty, or 32 bytes that tie the tree to another document (the conversion trees use the declaration hash) |
| `slot_id` | same rule as `institution_id`; unique within one tree. For the snapshot trees it is the ledger account id; an account has one currency, so it is one slot per account and currency |
| `amount` | 0 to 10^38 - 1 |
| `salt` | exactly 32 bytes |
| `payload` | a string of at most 4096 bytes in UTF-8. It must be empty when the kind is `liabilities` or `receivables` (the tree refuses to build otherwise, and a proof or export that breaks the rule is malformed). For the two conversion kinds it is the canonical JSON text of a conversion record (section 8.5) |

Worked example (file `tests/vectors/sumtree_v1.json`, tree `two-leaves`; kind `liabilities`,
institution `example-institution`, currency `JPY`, 0 minor units, instant
`2026-01-01T00:00:00.000000Z`, no binding, salt key `000102...1f`):

```
context                 ce298feaa458599c199eccf8c01e45c2e6ac6f890ba6df5cd2248e661ff0f002
salt of slot bob        9718cd5fb3a5b136cf09d6cfcaafd38796fd2208558f5b6fa6f45ac966d632be
leaf bob,   120000      cc2b75e2bd3b6c0fc552c792fc13535a1cbb9b3cf438338f5913bd1447c5f72c   (index 0)
leaf alice, 300000      cd9c5265f875d3d411ea0b8fede88f72084669cee3029adedaf16fc43bc9e374   (index 1)
top node, sum 420000    e718267f663d147859473a4adbfea0daf4e09c8b0a930a61587cac5024077084
root (count 2)          1f19f1f192942e28c1c8be36910ae201dcc647cda2b75b6043cb3387720c027d
```

The vector file also holds the exact input bytes of each of these five hashes.

### 6.2 Order and shape (D2, the stage A part of D22)

- **Order.** Leaves are sorted by their 32-byte leaf hash, ascending. The tree is therefore a
  function of the set of leaves: input order, creation time and storage order do not matter.
  Two leaves with the same slot id are refused when the tree is built.
- **Shape.** For a list of n nodes (hash, sum), n >= 1:
  `top(list) = list[0]` when n is 1; otherwise, with k the largest power of two smaller than n,
  `top(list) = node(top(list[0:k]), top(list[k:n]))`. This is the shape of RFC 6962 section 2.1
  (S4). It equals building level by level and carrying an unpaired last node up unchanged; a
  node is never paired with itself and nothing is padded.
- **Empty tree.** n = 0: `top_hash` is 32 zero bytes and the total is 0. The root hash still
  binds the context and the count 0. For the worked context the empty root is
  `75ea0f466b6231dd9e548d87000a7e10d9d90d091a9ae692476a53ecbfc5b64a`.
- **Count.** The leaf count is inside the root hash, so a proof for the wrong count cannot match.

### 6.3 Salts (D5)

A salt is derived, not stored: the HMAC of section 6.1 under `salt_key`, an operator secret that
is an explicit input (bytes, at least 32 long, S9). The library never creates, stores or logs the
key. The same account in two snapshots, two currencies or two kinds of tree gets different salts,
because the context differs.

- A holder receives their salt inside their inclusion proof, over whatever private channel the
  operator uses for proofs.
- An auditor receives every salt in the export of section 6.5 and so does not need the key.
- A public summary never contains a salt, a slot id or a payload.
- If the key leaks, anyone who also knows a slot id and holds a proof with a sibling hash can
  again test guesses, for every snapshot made with that key. Operators can use one key per
  snapshot series; key handling guidance is stage B.

Alternative: random salts stored per leaf. Rejected: it needs a new table, brings unrecorded
randomness into artifact generation, and adds nothing once the key is treated as an input.
Generation stays a pure function of ledger state, instant, institution id and key.

### 6.4 Inclusion proof and its check (D8)

A proof is a JSON object with exactly these keys (all integers are strings, section 4.1):

```
{ "format": "openreserve.sumtree.inclusion/1",
  "context": { "kind": ..., "institution_id": ..., "currency": ..., "minor_units": "0",
               "as_of": "2026-01-01T00:00:00.000000Z", "binding": "" },      binding: "" or 64 hex characters
  "leaf_count": "3",
  "leaf_index": "1",                                                        position in the sorted order, from 0
  "leaf": { "slot_id": ..., "amount": "120000", "salt": "<64 hex>", "payload": "" },
  "path": [ { "hash": "<64 hex>", "sum": "55000" }, ... ] }                 siblings, from the leaf upwards
```

A proof contains no root. The verifier is given the root it should trust:

```
verify_sum_tree_inclusion(proof, *, expected_root: str, expected_slot_id: str) -> Verification
```

`Verification` has `ok`, `reason` (a code from the list below, or `None`) and `details`; it is
true exactly when `ok` is. A malformed `expected_root` or `expected_slot_id` is a programming
error and raises `ValueError`. **No content of `proof` makes the function raise.**

Steps, in this order:

1. `proof` is an object with exactly the keys above and the format string; `context`, `leaf` and
   every path element have exactly their keys; every value has its type; hashes and the salt are
   64 lower-case hexadecimal characters; integers are canonical unsigned decimal text within
   their range; field rules of section 6.1 hold. If `proof` is not an object, or its format
   string is missing or different: `UNSUPPORTED_FORMAT`. Any other violation: `MALFORMED_PROOF`.
2. `leaf_count` is at least 1 (`EMPTY_TREE` when it is 0) and below 2^64 (`MALFORMED_PROOF`
   otherwise); `leaf_index < leaf_count` (`INDEX_OUT_OF_RANGE`).
3. The number of path elements equals the path length that the shape gives for
   (`leaf_index`, `leaf_count`) (`PATH_LENGTH_MISMATCH`).
4. Recompute the context digest and the leaf hash from the proof's own fields.
5. Fold upwards. At each step the side of the sibling follows from the index and the count, not
   from the proof. Each sibling sum is within range by step 1; the running sum after adding it
   must not exceed 10^38 - 1 (`SUM_OUT_OF_RANGE`).
6. Compute the root from the proof's context, `leaf_count`, the final sum and the final hash.
   It must equal `expected_root` (`ROOT_MISMATCH`).
7. The leaf's `slot_id` must equal `expected_slot_id` (`SLOT_MISMATCH`).

On success `details` holds what the root authenticated: the context, `leaf_count`, `total`,
`slot_id`, `amount`, `payload`, and `disclosed`, the list of (leaf count, sum) of each sibling
subtree from the leaf upwards. A caller compares `total` and `leaf_count` with the published
summary; `verify_snapshot_inclusion` (section 7) does that.

The side of each sibling and the path length, from the leaf upwards (equivalent to RFC 9162
section 2.1.3.2, S5):

```
sides(i, n):                       # 'R': the sibling is the right subtree, 'L': the left one
    out = []
    while n > 1:
        k = largest power of two smaller than n
        if i < k:  out.append(('R', n - k)); n = k
        else:      out.append(('L', k));     i -= k; n -= k
    return reversed(out)           # second element: number of leaves in that sibling subtree
```

### 6.5 Auditor export and full recomputation

```
{ "format": "openreserve.sumtree.export/1", "context": { ...as in a proof... },
  "leaves": [ { "slot_id": ..., "amount": ..., "salt": ..., "payload": ... }, ... ] }   in tree order
audit_sum_tree_export(export, *, expected_root: str) -> Verification
```

The audit checks the exact key sets and value rules (`MALFORMED_EXPORT`), rebuilds the tree from
the leaves (sorting them itself; duplicates and out-of-range values are `MALFORMED_EXPORT`),
compares the root (`ROOT_MISMATCH`) and then requires the listed order to be the tree order
(`NOT_IN_CANONICAL_ORDER`). On success `details` holds the head (`root`, `total`, `leaf_count`).
An export contains every balance and every salt: it is for auditors, not for publication.

The published head of a tree is `{"root": "<64 hex>", "total": "<decimal>", "leaf_count": "<decimal>"}`.

### 6.6 What a check shows, and what it does not

**Claim.** Assume nobody can find two different inputs with the same SHA-256 output. Fix a
published root. Take any set of proofs for pairwise different slot ids that the verifier accepts
against that root. Then the total bound in the root is at least the sum of the amounts in those
proofs.

Why. The root fixes the context, the count n, the total and the top hash. An accepted proof has
an index below n and exactly the path length the shape gives, so each value it computes sits at
a definite node position of the one shape for n. Going down from the top: all proofs agree on the
top value; a node hash fixes its four inputs, so all proofs passing through a node agree on the
(hash, sum) of both of its children, whether a proof computed that child itself or supplied it as
a sibling. So every position a proof touches has one value. Two proofs at the same leaf position
have the same leaf hash and therefore the same slot id; proofs for different slot ids sit at
different leaves. Going up: a touched leaf's sum is that proof's amount; a sibling nobody
computed has a sum of at least 0 (range check); each inner sum is the exact integer sum of its
children (no wrap-around, range check). So the total is at least the sum of the proven amounts.

This is the property the sum-only construction lacks (S2, S3). The test suite replays the
published counter-example against a sum-only node function (balances 1, 2, 3, 4; all four holders
accept a root that claims 4) and shows that the same forged sibling sums are refused here.

A check does **not** show: that an account nobody checked is in the tree with the right amount;
that every liability was recorded; that the operator gave every holder the same root (section 0,
point 4); that an identifier belongs to one holder only (point 5); anything about reserves.

The claim is about the tree the proofs are in. A snapshot has a liabilities tree and a separate
receivables tree (section 7), and an amount in the receivables tree says that the holder owes
it: it is not part of the liabilities total. An operator who books a customer it owes into the
receivables tree lowers the published liabilities by that amount, and the customer's proof is
still a genuine proof of a published root with the right amount. So a holder must also check
which tree the proof is in. The snapshot check and the conversion holder check (sections 7 and
8.6) therefore take the kind or side the holder expects from their own record, and refuse a
genuine proof of the other one (`KIND_MISMATCH`, `SIDE_MISMATCH`).

### 6.7 What a proof discloses (D6)

Decision: document the disclosure exactly, pin it with a test, and make the neighbour
unpredictable. Hiding amounts (splitting each balance into several shuffled leaves, or
commitments with range proofs) is not part of stage A; S2 and S3 describe both and their cost.

> A proof for one leaf shows its holder: the leaf count and the total (public anyway); and, for
> each step of the path, the hash and the exact sum of the sibling subtree, whose number of
> leaves follows from the index and the count. When a sibling subtree is a single leaf, that sum
> is another account's exact amount. This is the first step for most leaves. A proof never
> contains another account's identifier, salt or payload, and without the salt a leaf hash cannot
> be tested against a guessed identifier and amount. Holders who pool their proofs learn more
> amounts. Which leaf is the neighbour depends on salted hashes: it cannot be predicted from
> identifiers and it changes with every snapshot.

Pinned example: slots alice 300000, bob 120000, carol 55000, dave 9000 in the worked context.
The order is carol, dave, bob, alice; the root is
`0795a28fdc881a5065a683e173fabd140bec8da2a2d71479286bfed415944e79`; alice's proof (index 3) has
`disclosed == ((1, 120000), (2, 64000))`: bob's exact amount, and the sum of carol and dave.

### 6.8 Alternatives considered

- **Padding to a power of two with typed zero leaves.** Sound, but it adds a third node type and
  a rule against presenting padding as an account. The RFC 6962 shape needs neither and comes
  with a published verification procedure.
- **A level or position inside every node hash.** Not needed: the claim of 6.6 holds because the
  count is bound in the root and the verifier derives the shape from index and count.
- **Order by slot id.** With identifiers that can be listed, a holder could work out whose amount
  they see. Order by salted hash avoids that and still gives one canonical order.
- **Sums as decimal text inside node hashes.** Variable length; fixed 16 bytes are simpler in
  every language.
- **Zero-knowledge constructions.** They hide amounts but still do not show completeness, and a
  pure-Python standard-library implementation would be new cryptographic code. Out of scope.

### 6.9 Tests

Vectors: `tests/vectors/sumtree_v1.json`, produced by two differently structured design-time
implementations that agree, and recomputed by `tests/vectors/check_vectors_standalone.py`, which
uses only the standard library and imports nothing from the package (9 trees, 39 proofs).

| Tree (worked context) | Root | Total | Leaves |
|---|---|---|---|
| empty | `75ea0f466b6231dd9e548d87000a7e10d9d90d091a9ae692476a53ecbfc5b64a` | 0 | 0 |
| alice 300000 | `b122c6bf923cfe591ac253e3cfee5b1b03ba347132b5179620eda5d0bba5e1d4` | 300000 | 1 |
| alice, bob 120000 | `1f19f1f192942e28c1c8be36910ae201dcc647cda2b75b6043cb3387720c027d` | 420000 | 2 |
| alice, bob, carol 55000 ("abc") | `15999a7b45465f066c26266c9c0371f430116f646c810dc30196623faba05b3a` | 475000 | 3 |
| abc and dave 55000 | `f88f652b794a454653c105b1c3aa965f3e0fd35c4bfab31df0b5ca46f4fc7757` | 530000 | 4 |
| abc, dave 55000, erin 1 | `7e1e7790e82274a3455a377b2b87f5fdef354cefbcd6e065e605b410d0eb6f99` | 530001 | 5 |
| seven leaves 10^0 to 10^6 | `0e0020753f8dff41061a9754d117fe4ecb73e1c64f6dd1861c64ff57d8e8f066` | 1111111 | 7 |
| eight leaves 10^0 to 10^7 | `c8af4e44904cecdb80007e85fb61184a31ebee60b7c9f44039c15b465a1ad158` | 11111111 | 8 |
| nine leaves with 0, 2^63, 10^30, 10^37 | `892f206b96e85b53009682877dd06a19c66c89f05a76c9cc59b9c10b50508379` | 10000001000000000009223372036854775831 | 9 |

Required tests:

1. Every vector tree: context digest, salts, leaf hashes, order, top hash, head; every proof
   verifies and its `disclosed` list matches; sibling sums add up to total minus own amount and
   sibling leaf counts to count minus one.
2. Test (a), adapted: the legacy duplication case cannot be expressed (a second leaf for slot
   carol is refused when the tree is built); the trees for abc and for abc plus a different slot
   with carol's amount have different roots and totals 475000 and 530000; and a check of a
   holder's proof against a summary whose total was changed while the root was kept fails.
3. Each field of the context changes the root (six variants in the vector file), and so does
   another salt key.
4. The root does not depend on the input order; a duplicate slot id, a negative amount, an
   amount of 10^38, a salt of the wrong length, a `bool` or `float` amount, a total above the
   bound and a non-empty payload in a snapshot kind are refused when building; a proof of kind
   `liabilities` with a payload gives `MALFORMED_PROOF`.
5. Tampering with a genuine proof. Each row must give exactly the stated reason:

   | Change | Reason |
   |---|---|
   | amount plus 1; other slot id; one salt character; one sibling hash character; a sibling sum plus 1 or minus 1; two path steps swapped; kind, institution, currency, minor units, instant or binding changed | `ROOT_MISMATCH` |
   | last step removed; a zero step appended | `PATH_LENGTH_MISMATCH` |
   | a sibling sum set to 10^38 - 1 at a step where the running sum is at least 1 | `SUM_OUT_OF_RANGE` |
   | a sibling sum `-1`, `" 5"`, `05`, the number 5, or 10^38; a hash in upper case or with a space; an extra key; a missing key; `path` not a list; payload `x` in a snapshot proof | `MALFORMED_PROOF` |
   | another format string | `UNSUPPORTED_FORMAT` |
   | genuine proof, another expected slot id | `SLOT_MISMATCH` |
   | genuine proof, another expected root | `ROOT_MISMATCH` |

6. For every tree of 1 to 40 leaves and every leaf, every other pair (index, count) with count
   within 2 of the true count and index up to count plus 1 is refused (118,066 variants in the
   design-time run, none accepted).
7. For every tree of 1 to 130 leaves, the library's proofs equal those of the standalone
   implementation and verify in both (8,515 pairs in the design-time run).
8. The sum-only counter-example of 6.6.
9. Test (e), adapted. In a snapshot with at least four accounts, an attacker who knows another
   account's slot id, the full context and a range of amounts cannot reproduce any hash in a
   holder's proof by trying every amount with the salts available to them (the all-zero salt and
   the holder's own salt). The same account with the same balance has different leaf hashes in
   two snapshots. The pinned disclosure example of 6.7 holds.
10. A seeded mutation test: at least 10,000 random changes of a genuine proof and 1,000 of a
    genuine export (replace a value by one of a fixed list of odd values, drop a key, add a key,
    change one character). No call raises; a changed document is never accepted.
11. A size check without a time limit: 10,000 leaves build, and three proofs verify.

## 7. The liabilities snapshot (D4, with E1)

**Problem.** The generator makes one leaf per customer account whatever its balance. A customer
who is overdrawn becomes a negative leaf and lowers the published liabilities: with reserves 50
and balances +100 and -60 it publishes liabilities 40 and a positive coverage verdict (M4), while
the reserve calculation in `regulatory/payment_services_act.py` (lines 216-221) counts only
positive balances. One customer's debt must not cancel what is owed to another. Zero-balance
accounts become leaves as well, so a snapshot of the past changes when an account is opened later
(M7).

**Decision.**

1. For a currency and an instant, each customer account of that currency (the account type
   `USER`) with a **positive** settled balance is a leaf of the `liabilities` tree with that
   amount; each with a **negative** balance is a leaf of the `receivables` tree with the
   magnitude; an account with balance **zero has no leaf**. The two trees are never netted.
2. The ledger keeps allowing a customer account to go negative. Whether a product allows
   overdrafts is the operator's business rule, and an existing test relies on it (M9).
3. Coverage (section 9.2) uses the liabilities total only. The receivables tree lets a debtor
   check the amount recorded against them, and gives an auditor a second total to reconcile. It
   does not count as reserves. The claim of section 6.6 is a lower bound, which is the useful
   direction for liabilities; for receivables it does not stop an operator from listing debtors
   who do not exist.
4. What a snapshot contains follows the cut-off rules of section 4.3. Because only accounts with
   a non-zero balance at the instant have leaves, accounts opened later do not change an earlier
   snapshot, and the creation time of an account (a wall-clock value today) plays no role.
5. The legacy generator `ProofOfReservesGenerator` uses the same collection of balances and stops
   netting: accounts with a negative balance get no legacy leaf and are left out of
   `user_liabilities_total_cents`; their count and total magnitude are reported in two new
   fields, `user_receivables_count` and `user_receivables_total_cents` (summary keys
   `user_receivables_count`, `user_receivables_total`). `user_liabilities_count` keeps counting
   the accounts that have a legacy leaf. Zero-balance accounts keep their legacy leaf, so legacy
   roots change only for ledgers that have a negative customer balance.

Alternatives. Forbid overdrafts in the ledger: a business rule, and it breaks an existing test.
Refuse to make a snapshot when a negative balance exists: blocks the operators who most need to
show it. Leave negatives out and only report a number next to the root: the debtor could not
check anything. One tree with signed amounts: the lower bound of section 6.6 needs non-negative
sums.

**Interfaces** (module `openreserve/transparency/snapshot.py`):

```
collect_user_balances(ledger, currency, snapshot_at) -> list[tuple[str, int]]
        # every customer account of the currency with its signed settled balance, in storage order

LiabilitiesSnapshotGenerator(ledger, *, institution_id: str, salt_key: bytes)
    .generate(currency, snapshot_at) -> LiabilitiesSnapshot

LiabilitiesSnapshot
    .liabilities, .receivables          # the two SumTree objects
    .to_public_summary() -> dict
    .summary_hash() -> str              # SHA-256 of the canonical JSON of the summary
    .proof_for(account_id) -> dict      # the inclusion proof from whichever tree holds the account;
                                        # KeyError when the account has no leaf
    .auditor_export() -> dict

verify_snapshot_inclusion(proof, summary, *, expected_slot_id, expected_kind) -> Verification
audit_snapshot_export(export, *, expected_summary_hash) -> Verification
```

Public summary:

```
{ "format": "openreserve.liabilities-snapshot/1",
  "institution_id": "example-institution", "currency": "JPY", "minor_units": "0",
  "snapshot_at": "2026-01-01T00:00:00.000000Z",
  "inclusion_rule": "SETTLED_AT_OR_BEFORE_SNAPSHOT",
  "population_rule": "USER_ACCOUNTS_WITH_NONZERO_BALANCE",
  "liabilities": { "root": ..., "total": "475000", "leaf_count": "3" },
  "receivables": { "root": ..., "total": "0", "leaf_count": "0" } }
```

`verify_snapshot_inclusion` checks, in order: the summary has exactly these keys and valid
values (`MALFORMED_SUMMARY`); the proof's kind is `liabilities` or `receivables` and its context
equals the summary's institution, currency, minor units and instant, with an empty binding
(`CONTEXT_MISMATCH`); `verify_sum_tree_inclusion` against the root of that kind in the summary
(its reason is passed on); the total and leaf count authenticated by the root equal those stated
in the summary (`TOTAL_MISMATCH`, `COUNT_MISMATCH`); and last, the proof's kind is
`expected_kind`, the tree the holder expects from their own record: `liabilities` when the
institution owes them the amount, `receivables` when they owe it (`KIND_MISMATCH`, with the kind
found in `details`). Because this comes last, `KIND_MISMATCH` means that the proof is genuine
for the summary's tree of the other kind. An `expected_kind` that is not one of the two kinds
raises `ValueError`. The holder then compares the amount in `details` with what they believe
their balance was, and `summary_hash` in `details` with the published hash of the summary; the
library cannot do that for them.

Auditor export: `{"format": "openreserve.liabilities-snapshot.export/1", "summary": {...},
"liabilities": <tree export>, "receivables": <tree export>}`. `audit_snapshot_export` checks the
key sets, the summary hash (`SUMMARY_HASH_MISMATCH`), both tree exports against the summary's
roots (the tree reason prefixed with `LIABILITIES_` or `RECEIVABLES_`), their contexts
(`LIABILITIES_CONTEXT_MISMATCH`, `RECEIVABLES_CONTEXT_MISMATCH`), that each tree has the total
and leaf count stated in the summary (`LIABILITIES_HEAD_MISMATCH`, `RECEIVABLES_HEAD_MISMATCH`),
and that no slot id appears in both trees (`SLOT_IN_BOTH_TREES`).

**Tests.** Expected values are in `tests/vectors/snapshot_expected_v1.json`.

1. E4: before the fix, a strict expected-failure test for M4 (published liabilities must be 100,
   not 40, and the verdict with reserves 50 must be negative).
2. Ledger with fixed account ids alice, bob, carol holding 300000, 120000, 55000 JPY, all settled
   at 2026-01-01T00:00Z, institution `example-institution`, key `000102...1f`, snapshot at the
   same instant: liabilities root
   `15999a7b45465f066c26266c9c0371f430116f646c810dc30196623faba05b3a`, total 475000, 3 leaves;
   receivables root `f7b3370b9761b2dcdbb71edfb057c32c7522b2ec5ac81665720716e2b5afd79c`, total 0,
   0 leaves; summary hash `c981cf21c98a294c7ba4db280c0939d9ec984c82b3b0c82570d0fa1f8f6dd4ed`.
3. Ledger with ids a (+100) and b (-60): liabilities root `3c94a9c2...b724`, total 100, 1 leaf;
   receivables root `bf7af395...b50a`, total 60, 1 leaf; summary hash `48756d5c...263d`. On the
   same ledger with reserves 50 the legacy generator reports liabilities 100 (40 before),
   receivables count 1 and total 60, and `is_solvent` False (True before).
4. An account with balance zero has no leaf and `proof_for` raises `KeyError`. A pending transfer
   to a customer is not in their leaf.
5. Regenerating the snapshot of test 2 after a new account was opened and funded at a later
   instant gives the same summary hash.
6. The same instant passed as `2026-01-01T09:00+09:00` gives the same summary hash.
7. Every holder's proof passes `verify_snapshot_inclusion` with the kind they expect; the
   debtor's proof has kind `receivables`. A summary with a changed total and the unchanged root
   gives `TOTAL_MISMATCH`; with a changed count `COUNT_MISMATCH`; a proof from another currency
   or instant gives `CONTEXT_MISMATCH`. A customer who is owed 300000 but booked into the
   receivables tree with that magnitude (published liabilities 120000 instead of 420000) gets
   `KIND_MISMATCH` with `expected_kind="liabilities"`; the same proof passes with
   `expected_kind="receivables"`, as a debt of 300000.
8. The public summary contains no salt and no account id (searched recursively), and no float.
9. The auditor export passes; with one amount changed it gives `LIABILITIES_ROOT_MISMATCH`.
10. A key shorter than 32 bytes, a key that is not `bytes`, or an empty institution id raises
    `ValueError`. Generation works with `datetime.now` replaced by a function that raises.

## 8. Conversion evidence (R2: D10 to D14)

**Problem.** `TransactionBuilder.fx_convert` books both currencies to one account, and
`Ledger.post` rejects any entry whose currency differs from its account's, so a conversion can
never be posted (M2 c). A rate can only be kept as free-text metadata. The library has no
rounding rule for money and no exact rational arithmetic (M11).

**Scope.** A fixed-rate conversion of every balance of one source currency into one target
currency, at one declared rate and rule, as of one cut-off instant, inside one institution's
ledger. Several source currencies are several declarations. Conversion between two old currencies
through the new one (S1 Article 4(4)) and fresh-start variants are out of scope.

### 8.1 The declaration (D11, D12)

A declaration is a canonical JSON document (section 4.2). Its SHA-256 is the `declaration_hash`,
which identifies the conversion event everywhere else.

```
{ "format": "openreserve.conversion.declaration/1",
  "declaration_id": "fictional-changeover/JPY",              label, 1 to 255 bytes, no control characters
  "source": { "code": "JPY", "minor_units": "0" },
  "target": { "code": "FICTIONAL_W", "minor_units": "2" },
  "rate": { "text": "147.25", "quotation": "SOURCE_PER_TARGET" },
  "rate_significant_figures": null,                           or a count such as "6"
  "rounding": { "mode": "HALF_AWAY_FROM_ZERO", "quantum_minor_units": "1", "level": "ACCOUNT" },
  "cutoff_at": "2099-12-31T15:00:00.000000Z",
  "reference_document_sha256": null }                         or the SHA-256 of the declaring document
```

Rules (a violation raises `DeclarationError` when a declaration is loaded):

- **Rate text.** It must fully match `(0|[1-9][0-9]{0,29})(\.[0-9]{1,30})?` and be greater than
  zero. The text is kept verbatim, trailing zeros included (`340.750` stays `340.750`). Its value
  is the exact fraction `digits / 10^(number of fractional digits)`, built from integers. A
  `float`, an `int`, a `Decimal`, a `Fraction`, an exponent, a sign, a space, a leading zero or a
  digit separator is refused. Examples: `0.1` is 1/10; `1.95583` is 195583/100000; `340.750` is
  1363/4.
- **Quotation.** Exactly one of two values, always in major units:
  `SOURCE_PER_TARGET`: units of the source currency for one unit of the target currency. This
  is how the euro rates were adopted (S1 Article 4(1): one unit of the new currency expressed in
  each old currency). `TARGET_PER_SOURCE`: units of the target currency for one unit of the
  source currency. The declared text and direction are used as they are. The library has no
  operation that inverts a rate or stores a decimal reciprocal (S1 Article 4(3), recital 10); a
  rate declared one way is converted by exact division, the other way by exact multiplication.
- **Significant figures.** When `rate_significant_figures` is not `null`, the rate text must have
  exactly that many significant figures, counted from the left starting at the first non-zero
  figure (S1 recital 12): `1.95583`, `340.750`, `0.787564` and `1936.27` each have six;
  `0.0064` has two; `100` has three.
- **Rounding.** `mode` is `HALF_AWAY_FROM_ZERO` or `HALF_EVEN` and must be stated; there is no
  default. `quantum_minor_units` must be `"1"` and `level` must be `"ACCOUNT"` in this format
  version; other values are refused as unsupported.
- **Currencies.** Codes follow the rule of section 6.1; minor units are 0 to 30; source and
  target differ.
- **Cut-off.** `cutoff_at` is the canonical text of the instant whose settled balances are
  converted (inclusive, section 4.3).
- `reference_document_sha256` is the hash of the document in which the rate and rule were
  declared, or `null` when there is none. A hash shows neither who published the document nor
  when; that is stage B.

### 8.2 Arithmetic (D11, D12)

With `x` the signed amount in source minor units, `ds` and `dt` the minor units of source and
target, and `rate` the exact fraction:

```
factor = 10^(dt - ds) / rate      for SOURCE_PER_TARGET
factor = 10^(dt - ds) * rate      for TARGET_PER_SOURCE
z = x * factor                    exact fraction: the unrounded amount in target minor units
y = Round(z)                      integer: the converted amount in target minor units
e = y - z                         exact fraction: the rounding residue, |e| <= 1/2
```

All of it is integer and `fractions.Fraction` arithmetic. `|x|` and `|y|` must not exceed
10^38 - 1 (`ConversionError` otherwise).

`Round(z)` for `z = n/d` with `d > 0`: let `q, r = divmod(n, d)` (floor division, so
`0 <= r < d`). If `2r < d` the result is `q`. If `2r > d` it is `q + 1`. If `2r = d` (a tie):
`HALF_AWAY_FROM_ZERO` gives `q + 1` when `n > 0` and `q` when `n < 0`; `HALF_EVEN` gives `q`
when `q` is even and `q + 1` when it is odd.

Why these rules.

- S1 Article 5 rounds to the nearest sub-unit and rounds an exact half up. It speaks of amounts
  to be paid or accounted for and says nothing about negative numbers. Applying it to the
  magnitude gives `HALF_AWAY_FROM_ZERO`. **For negative amounts this is a design choice, not
  something the source states.**
- Both modes are symmetric: `Round(-z) = -Round(z)`. A debt of x and a claim of x convert to the
  same magnitude, and the two opposite entries of a double-entry posting stay opposite after
  conversion. A mode without this property (ties always toward plus infinity) is refused for
  that reason.
- Rounding happens once, at the end, per account. Keeping exact fractions until then is the
  "more accuracy in intermediate computations" that S1 recital 11 leaves untouched.
- The mode is part of the declaration and has no default, so a verifier never guesses.

Worked examples (`tests/vectors/conversion_v1.json`: 9 declarations, 78 conversions).

| Declaration | factor | x | z | y | e |
|---|---|---|---|---|---|
| `1.95583` SOURCE_PER_TARGET, 2 and 2 minor units, HALF_AWAY_FROM_ZERO | 100000/195583 | 10000 | 1000000000/195583 (about 5112.92) | 5113 | 15879/195583 |
| same | | -10000 | -1000000000/195583 | -5113 | -15879/195583 |
| same | | 195583 | 100000 | 100000 | 0 |
| same | | 100000000000 | 10000000000000000/195583 | 51129188120 | 73960/195583 |
| `2` SOURCE_PER_TARGET, HALF_AWAY_FROM_ZERO | 1/2 | 1, 3, 5, 7, -1, -3, -5, -7 | odd/2 (ties) | 1, 2, 3, 4, -1, -2, -3, -4 | |
| `2` SOURCE_PER_TARGET, HALF_EVEN | 1/2 | 1, 3, 5, 7, -1, -3, -5, -7 | odd/2 (ties) | 0, 2, 2, 4, 0, -2, -2, -4 | |
| `0.5` TARGET_PER_SOURCE (the same factor, declared the other way) | 1/2 | as above | | as above for each mode | |
| `147.25` SOURCE_PER_TARGET, 0 to 2 minor units, HALF_AWAY_FROM_ZERO | 400/589 | 300000 | 120000000/589 | 203735 | -85/589 |
| same | | 120000 | 48000000/589 | 81494 | -34/589 |
| same | | 55000 | 22000000/589 | 37351 | -261/589 |
| same | | -60 | -24000/589 | -41 | -149/589 |
| `0.0068` TARGET_PER_SOURCE, 18 to 2 minor units | 17/25000000000000000000 | 10000000000000000000 | 34/5 | 7 | 1/5 |
| same | | 735294117647058823 | just below 1/2 | 0 | |
| same | | 735294117647058824 | just above 1/2 | 1 | |
| `147.25` SOURCE_PER_TARGET, 0 to 0 minor units | 4/589 | 73 | 292/589 | 0 | -292/589 |
| same | | 74 | 296/589 | 1 | 293/589 |
| `1.25` SOURCE_PER_TARGET, 2 to 18 minor units, HALF_EVEN | 8000000000000000 | 12345 | 98760000000000000000 | 98760000000000000000 | 0 |

Why a rate must not be inverted: `1/1.95583` to six significant figures is `0.511292`. For
x = 100000000000 (one thousand million major units) exact division gives 51129188120, while
multiplying by the rounded inverse gives 51129200000: a difference of 11880 minor units. This is
the inaccuracy "for large amounts" of S1 recital 10.

### 8.3 Posting through clearing accounts (D14)

Each account has one currency and each currency journal of a transaction must sum to zero. A
conversion of the signed balance `x` of one account is therefore one transaction with four
entries, in this order:

```
(source account,           -x)   source currency
(source clearing account,  +x)   source currency
(target clearing account,  -y)   target currency
(target account,           +y)   target currency
```

When `y` is 0 the last two entries are left out; when `x` is 0 nothing is posted. Both legs are in
one transaction, so the store writes them together or not at all.

`TransactionBuilder.fx_convert` is repaired on the same model, without any rate logic: its
signature becomes `fx_convert(from_account_id, from_amount, to_account_id, to_amount, *,
from_clearing_account_id, to_clearing_account_id)`. The old fifth positional parameter is gone,
so the call that could never be posted now fails when it is written, with a `TypeError`.

Declared conversions use:

```
post_conversion(ledger, declaration, *, source_account_id, target_account_id,
                clearing_source_account_id, clearing_target_account_id,
                effective_at, initiator_id) -> Transaction | None
post_conversion_batch(ledger, declaration, *, pairs, clearing_source_account_id,
                clearing_target_account_id, effective_at, initiator_id) -> ConversionBatchResult
```

The transaction:

- id `conv_` + hex of `SHA-256("openreserve.conversion.v1" || 0x01 || declaration_hash (32 bytes) || lp(utf8(source_account_id)))`;
  entry ids `<transaction id>:<sequence>`;
- purpose code `CURRENCY_CONVERSION`; started and settled at `effective_at`; status SETTLED;
- one metadata pair with the key `openreserve.conversion/1` whose value is the canonical JSON
  text of `{"declaration_hash", "source_account_id", "source_amount", "target_account_id",
  "target_amount"}` (signed decimal strings). The record is a convenience: every later check
  recomputes it from the entries and the declaration.

Checks, in this order:

1. `effective_at` is aware and later than the cut-off; the accounts exist; source and target
   account have the declared currency codes and minor units and the same account type; the two
   clearing accounts have the type `CLEARING` and the right currencies; the four accounts are
   different (`ConversionError`).
2. **Replay.** If a transaction with the derived id exists, the source account was already
   converted under this declaration: `ConversionReplayError`. The id does not depend on the
   target, so converting the same source into a second target is refused as well. Below that, the
   store's primary key refuses a second transaction with the same id (`IntegrityError`).
3. The source account has no pending transaction (`ConversionBlockedError`).
4. The target account has no entry at all, settled or pending (`ConversionBlockedError`).
5. The settled balance of the source account over all time equals its balance at the cut-off:
   nothing happened after the cut-off (`ConversionBlockedError`).
6. `x` is the balance at the cut-off; if it is 0, return `None`.

`post_conversion_batch` takes pairs (source, target), works in ascending order of the source id,
and can be run again after an interruption: a source that already has its conversion transaction
with the same target is skipped and reported; with another target it raises
`ConversionReplayError`. The result lists the posted, skipped and zero-balance sources.

Any account type can be converted this way (reserve and own accounts too); the evidence of
section 8.5 covers the customer accounts.

### 8.4 Closing and the rounding residue (D13)

The source-currency balances of a ledger sum to zero. Once every account of the source currency
has been converted, the source clearing account is therefore back at zero. In the target currency
the clearing account holds `-(sum of all y)`, which is in general not zero, because each account
was rounded separately. This is the bookable, integer rounding difference of the whole ledger.

```
close_conversion(ledger, declaration, *, clearing_source_account_id, clearing_target_account_id,
                 residue_account_id, effective_at, initiator_id) -> int
```

1. Every account of the source currency, the source clearing account included, has a settled
   balance of 0 at `effective_at` (`ConversionIncompleteError` otherwise).
2. `S` is the sum of the target-clearing entries of this declaration's conversion transactions
   (found through the derived ids). The balance of the target clearing account must equal `S`
   (`ConversionError` otherwise: the clearing account is shared with something else; use one
   clearing account per currency for each declaration).
3. If `S` is not 0, one closing transaction is posted: `(target clearing account, -S)`,
   `(residue account, +S)`; id `convres_` + hex of
   `SHA-256("openreserve.conversion.v1" || 0x02 || declaration_hash)`; purpose code
   `CURRENCY_CONVERSION_CLOSING`; metadata key `openreserve.conversion.closing/1` with
   `{"declaration_hash", "residue_account_change"}`. The residue account has the existing type
   `FX_GAIN_LOSS` and the target currency. Running the function again changes nothing.
4. It returns `S`, the change of the residue account: positive when the institution gains from
   rounding, negative when it loses.

**Who bears it.** The institution: the residue account is its own. The manifest states
`"bearer": "INSTITUTION"`; no other value exists in this format version. A holder's converted
amount is always `Round(z)` for that holder; it is never adjusted to make totals agree.

No new account type is introduced; the package already groups roles under its existing types.

Besides the booked amount, the manifest reports for the customer liabilities and, separately,
for the customer receivables: the exact total `factor * sum(x)` as a fraction, that total
rounded, the difference `sum(y) - Round(factor * sum(x))` (an integer), and the exact sum of the
residues `sum(y) - factor * sum(x)` (a fraction). Fractions are written as
`{"num": "<signed decimal>", "den": "<decimal>"}` in lowest terms with a positive denominator.
The residue of a single account is not stored separately: it is determined by that account's x
and y and the declaration, every check recomputes it, and the holder check returns it as an exact
fraction.

### 8.5 The evidence (D10)

```
build_conversion_evidence(ledger, declaration, *, institution_id, salt_key,
        clearing_source_account_id, clearing_target_account_id, residue_account_id,
        effective_at) -> ConversionEvidence
ConversionEvidence: .manifest, .manifest_hash, .bundle_for(source_account_id), .auditor_export()
```

It requires a closed conversion (section 8.4) and that every customer account with a non-zero
balance at the cut-off has its conversion transaction with the right amounts
(`ConversionIncompleteError` otherwise). It reads the balances, targets and the closing amount
from the ledger and hands them to a function that needs no ledger:

```
assemble_conversion_evidence(declaration, *, institution_id, salt_key, effective_at, accounts,
        conversions, other_target_balances=(), residue_account_change=0) -> ConversionEvidence
        # conversions: (source slot id, signed source amount, target slot id) per converted account
        # other_target_balances: (slot id, signed amount) of customer accounts in the target
        #                        currency that are not targets of this conversion
```

The assembler computes every converted amount itself from the declaration; it never takes one as
input. It is also what makes evidence testable for a currency the ledger cannot hold, such as
`FICTIONAL_W`. Either way six trees are built with the construction of section 6:

| Tree | Kind | Currency | Instant | Binding | Leaves |
|---|---|---|---|---|---|
| pre, liabilities | `liabilities` | source | cut-off | none | (source account, x) for x > 0 |
| pre, receivables | `receivables` | source | cut-off | none | (source account, -x) for x < 0 |
| records, liabilities | `conversion-liabilities` | target | `effective_at` | declaration hash | one per pre leaf: slot = source account, amount = y, payload = the record |
| records, receivables | `conversion-receivables` | target | `effective_at` | declaration hash | the same for the receivables, with magnitudes |
| post, liabilities | `liabilities` | target | `effective_at` | none | every customer account of the target currency with a positive balance |
| post, receivables | `receivables` | target | `effective_at` | none | the same with a negative balance |

The pre and post trees are the ordinary snapshot trees of section 7. A record payload is the
canonical JSON text `{"source_amount":"<magnitude of x>","target_slot_id":"<target account>"}`.
A record whose `y` is 0 has a leaf with amount 0 in the records tree and no leaf in the post
tree.

Why a tree of records: it binds the mapping from each source to its target and amount to a
published root, and its total binds `sum(y)`. If two sources were pointed at one target and the
post tree held only the real balances, it would hold that amount once and its total would fall
below the records total, which anyone can see in the manifest. The manifest alone does not
show it when another post leaf covers the gap: under `MIXED` an unrelated balance, under either
scope a leaf made up for the purpose. Then the holder whose amount is not in the shared target
leaf notices (`POST_AMOUNT_MISMATCH`); when both converted amounts are equal, neither holder
does, and only the auditor's check notices (`TARGET_NOT_UNIQUE`).

`effective_at` is the instant of the post snapshot, and the manifest states it. It is not tied
to the instant at which the conversions were posted: the evidence builds for any instant after
the cut-off at which every account of the source currency is at zero and every target holds
exactly its converted amount, so a later instant can be used as long as nothing has changed
those balances.

The manifest (canonical JSON; `manifest_hash` is its SHA-256):

```
{ "format": "openreserve.conversion.manifest/1",
  "institution_id": ...,
  "declaration": { ...the declaration document... },
  "declaration_hash": ...,
  "effective_at": "2099-12-31T15:00:01.000000Z",
  "accounts": { "clearing_source": ..., "clearing_target": ..., "residue": ... },
  "liabilities": {
      "pre":        { "root": ..., "total": "475000", "leaf_count": "3" },
      "conversion": { "root": ..., "total": "322580", "leaf_count": "3" },
      "converted_nonzero_count": "3",
      "exact_total": { "num": "10000000", "den": "31" },
      "rounded_exact_total": "322581",
      "reconciliation_difference": "-1",
      "sum_of_residues": { "num": "-20", "den": "31" } },
  "receivables": { ...the same seven keys... },
  "post": { "scope": "CONVERSION_ONLY",
            "liabilities": { "root": ..., "total": "322580", "leaf_count": "3" },
            "receivables": { "root": ..., "total": "41", "leaf_count": "1" } },
  "ledger_closing": { "bearer": "INSTITUTION", "residue_account_change": "1" } }
```

`converted_nonzero_count` is the number of records of that side whose converted amount is not 0,
which is the number of post leaves they account for. `accounts` names the two clearing accounts
and the residue account used in the ledger.

`post.scope` is computed, not chosen: `CONVERSION_ONLY` when the post trees hold exactly the
targets of this conversion with their converted amounts; `MIXED` when other customer balances in
the target currency exist at that instant. There is no signature in this format; a signature can
later be made over `manifest_hash` without changing the manifest (D19).

Holder bundle and auditor export:

```
{ "format": "openreserve.conversion.holder-bundle/1", "manifest_hash": ..., "side": "liabilities" | "receivables",
  "pre_proof": <inclusion proof>, "record_proof": <inclusion proof>, "post_proof": <inclusion proof> | null }

{ "format": "openreserve.conversion.export/1", "manifest": {...},
  "liabilities": { "pre": <tree export>, "conversion": <tree export>, "post": <tree export> },
  "receivables": { "pre": ..., "conversion": ..., "post": ... } }
```

### 8.6 The three checks

All three return a `Verification`, require exact key sets, and require the whole document to fit
the canonical value profile before anything else; no content makes them raise.

**Anyone, from the manifest alone:** `verify_conversion_manifest(manifest)`.

1. Shape, formats, field rules; the declaration is valid (`MALFORMED_MANIFEST`); its hash equals
   `declaration_hash` (`DECLARATION_HASH_MISMATCH`); `effective_at` is later than the cut-off
   (`EFFECTIVE_NOT_AFTER_CUTOFF`).
2. For each side: the pre and records trees have the same leaf count and
   `converted_nonzero_count` does not exceed it (`COUNT_MISMATCH`); `exact_total` equals
   `factor * pre.total` and `rounded_exact_total` is its rounding (`EXACT_TOTAL_MISMATCH`);
   `reconciliation_difference` and `sum_of_residues` follow from the records total
   (`RESIDUE_MISMATCH`); twice the magnitude of `sum_of_residues` does not exceed the leaf count,
   because each residue is at most one half (`RESIDUE_OUT_OF_BOUND`).
3. Under `CONVERSION_ONLY` the post total equals the records total and the post leaf count equals
   `converted_nonzero_count`; under `MIXED` neither is smaller (`POST_TOTAL_MISMATCH`).

The roots themselves, and `residue_account_change`, cannot be checked from the manifest alone.

**A holder:** `verify_conversion_bundle(bundle, manifest, *, expected_manifest_hash,
expected_source_slot_id, expected_side)`. `expected_side` is the side the holder expects from
their own record: `liabilities` when the institution owed them their balance at the cut-off,
`receivables` when they owed it; another value raises `ValueError`. The manifest passes the
check above and its hash is the published one
(`MANIFEST_HASH_MISMATCH`); the bundle has its keys (`MALFORMED_BUNDLE`) and names the same hash
in its `manifest_hash` field (`MANIFEST_HASH_MISMATCH`). Then, for the bundle's side:

1. `pre_proof` has exactly the pre context (`PRE_CONTEXT_MISMATCH`) and verifies against the pre
   root for the holder's slot (tree reason prefixed with `PRE_`). Its amount is `x`.
2. `record_proof` has exactly the records context, declaration hash included
   (`RECORD_CONTEXT_MISMATCH`), and verifies against the records root for the same slot
   (`RECORD_`). Its payload is a canonical record (`RECORD_PAYLOAD_MALFORMED`) whose
   `source_amount` equals `x` (`SOURCE_AMOUNT_MISMATCH`). Its amount equals `Round(x * factor)`
   recomputed from the declaration (`CONVERTED_AMOUNT_MISMATCH`).
3. If that amount is 0, `post_proof` must be `null` (`POST_PROOF_UNEXPECTED`). Otherwise it must
   be present (`POST_PROOF_MISSING`), have exactly the post context (`POST_CONTEXT_MISMATCH`),
   verify against the post root for the record's `target_slot_id` (`POST_`), and state the same
   amount (`POST_AMOUNT_MISMATCH`).
4. Last, the bundle's side is `expected_side` (`SIDE_MISMATCH`, with the side found in
   `details`). Because this comes last, `SIDE_MISMATCH` means that the bundle is genuine for the
   other side of the published manifest: an operator who converts a balance it owed on the
   receivables side lowers the published liabilities totals by that amount, and the manifest,
   the holder's proofs and the auditor's recomputation are all consistent with that.

Each proof must also lead to a root whose total and leaf count are those the manifest states
(`PRE_HEAD_MISMATCH`, `RECORD_HEAD_MISMATCH`, `POST_HEAD_MISMATCH`), so that the figures of
the manifest check are the figures bound in the roots.

On success `details` holds the side, the source amount, the converted amount, the exact `z`
and `e`, and the target slot. Every figure is a magnitude: on the side `receivables` the values
`x`, `y`, `z` and `e` of section 8.2 are negative (`y` and `e` can be 0), `details` holds
their magnitudes, and the sign is the side. The
holder still has to recognise the source and target identifiers as their own, and to compare
the source amount with their own record of their balance.

**An auditor:** `audit_conversion_export(export, *, expected_manifest_hash)`. The manifest check
and the hash; each of the six tree exports has exactly its context (`PRE_`, `CONVERSION_` or
`POST_` + `CONTEXT_MISMATCH`), passes the tree audit (the same prefixes) and gives the head stated
in the manifest (`..._HEAD_MISMATCH`); no slot is in both pre trees or in both post trees
(`PRE_SLOT_IN_BOTH_TREES`, `POST_SLOT_IN_BOTH_TREES`); the records and the pre leaves of a side
have the same slots (`RECORDS_DO_NOT_MATCH_PRE_SNAPSHOT`); each record payload is canonical
(`RECORD_PAYLOAD_MALFORMED`), repeats the pre amount (`SOURCE_AMOUNT_MISMATCH`) and its leaf
carries `Round(x * factor)` (`CONVERTED_AMOUNT_MISMATCH`); no target appears twice across both
sides (`TARGET_NOT_UNIQUE`); each record with a non-zero amount has its post leaf with that amount
(`POST_AMOUNT_MISMATCH`); under `CONVERSION_ONLY` the post trees have no other leaf
(`POST_HAS_UNCONVERTED_LEAVES`); and `converted_nonzero_count` is the number of records of that
side with a non-zero amount (`COUNT_MISMATCH`).

With access to the ledger, the strongest check is to run `build_conversion_evidence` again and
compare manifest hashes: generation is deterministic, and it re-checks the closing.

### 8.7 Alternatives considered

- **Fix the quotation to one direction.** Rejected: the euro precedent declares source units per
  target unit; accepting only the other direction would force exactly the inversion that S1
  forbids. Both directions are exact here.
- **Half-up toward plus infinity for negative amounts.** Rejected: not symmetric, so a posting's
  two sides would no longer cancel at a tie.
- **Round per holder or per contract.** The declaration has a `level` field for it, but only
  `ACCOUNT` exists in this version; the ledger has no notion of a holder with several accounts.
- **A new account type for the residue.** Rejected: `FX_GAIN_LOSS` exists for exactly this kind of
  difference, and a new type would make existing files unreadable by older code for no gain.
- **One transaction for the whole batch.** Atomic, but it needs four entries per account in one
  object. One transaction per account with a derived id scales, can be resumed, and gives every
  holder their own transaction.
- **An event id that includes the pre-snapshot root.** Rejected: a second run on a changed
  snapshot would then count as a new event, and the same account could be converted twice.
- **No tree of records, only pre and post proofs.** Rejected: the mapping from source to target
  and the total of converted amounts would not be bound to anything published.

### 8.8 Tests

Vectors: `conversion_v1.json`, `conversion_evidence_v1.json`,
`conversion_ledger_fixture_v1.json` in `tests/vectors/`.

1. Rate text: the 13 valid texts with their fractions and significant figures; the 20 invalid
   texts; a `float`, an `int`, a `Decimal` and a `Fraction` are refused.
2. The 7 refused declarations (wrong number of significant figures, unknown quotation, unknown
   mode, equal currencies, a JSON number as rate, quantum 5, level HOLDER).
3. The 9 declarations: canonical bytes, hash, factor; all 78 conversions (x, z, y, e).
4. `Round` against a second implementation that works on sign and magnitude with integers only:
   at least 100,000 random fractions with constructed ties, both modes; symmetry in sign; the
   residue never exceeds one half.
5. The inverse-rate case: 51129188120, and 51129200000 through the rounded inverse.
6. The four batch totals of the vector file, for example magnitudes 300000, 120000, 55000 at
   `147.25`: converted 322580, exact 10000000/31, rounded 322581, difference -1, sum of residues
   -20/31; and for random batches `sum(y) - sum(z) = sum(e)` and `sum(z) = factor * sum(x)`
   exactly.
7. Test (c), adapted to the new `fx_convert`: 15,000 JPY against 100 USD posts and settles; the
   balances are 0 and 10000 minor units for the holder, +15000 on the JPY clearing account and
   -10000 on the USD clearing account; the old call with five positional arguments raises
   `TypeError`.
8. The ledger fixture (stand-in currencies JPY and USD and a rate of `9` that is not a real rate,
   because the ledger's currencies are a closed set until the registry reaches the ledger): the
   entries of the six conversions equal the fixture (for alice: -300000, +300000, -3333333,
   +3333333); the transaction ids equal the fixture; both clearing accounts hold 0 and +1 before
   closing; `close_conversion` returns 1 and leaves both at 0 and the residue account at 1; all
   target-currency balances sum to 0; the source balances at the cut-off are unchanged; the
   manifest equals the fixture's, hash
   `aced3b5d7dba19e83eb8b1e8fba2b85441864dacbf53c501d22d6c91fb5067e4`; building the evidence twice
   gives the same hash.
9. Replay: the same pair again and the same source with a fresh target both raise
   `ConversionReplayError`; a hand-made transaction with the derived id is refused by the store;
   running the batch again posts nothing.
10. Blocks: a pending transaction on the source; a target with an entry; a settled entry on the
    source after the cut-off; `effective_at` not after the cut-off; a wrong currency; a clearing
    account of another type. Closing with one account not converted, and building evidence before
    closing, raise `ConversionIncompleteError`.
11. The three evidence examples: manifest hashes
    `fe594a86cad764efbec91e827646da25850ef76014a96d4749a73598d9678ac0` (JPY to `FICTIONAL_W` with
    2 minor units), `dde3e7c0066f173a56a0530b77bf6e0f0eaf9dded6936f13299d0ab890de5d29` (0 minor
    units, one amount converts to 0) and
    `52f1ff42c2702dc465d087e36d5c7e353ae09860c0ca3cc16afcabc50e9b82ec` (scope `MIXED`); every
    bundle verifies with the listed details; every export passes the audit.
12. Changes to a genuine bundle, each with exactly this reason: post amount off by one
    `POST_ROOT_MISMATCH`; record amount off by one `RECORD_ROOT_MISMATCH`; pre amount changed
    `PRE_ROOT_MISMATCH`; the bundle of another holder `PRE_SLOT_MISMATCH`; the post proof of
    another holder `POST_SLOT_MISMATCH`; post proof removed `POST_PROOF_MISSING`; pre proof with
    another instant `PRE_CONTEXT_MISMATCH`; record proof bound to another declaration
    `RECORD_CONTEXT_MISMATCH`; side swapped `PRE_CONTEXT_MISMATCH`; a manifest with a changed
    total `RESIDUE_MISMATCH`; with the quotation inverted `DECLARATION_HASH_MISMATCH`; another
    expected manifest hash `MANIFEST_HASH_MISMATCH`.
13. Cheating that keeps the manifest consistent with itself: one holder of three short-changed by
    one minor unit is caught by the manifest check alone (`RESIDUE_OUT_OF_BOUND`); one holder
    over-credited by one is caught by that holder (`CONVERTED_AMOUNT_MISMATCH`) and by the
    auditor, while another holder's bundle still verifies; two sources pointed at one target give
    `POST_TOTAL_MISMATCH` under both scopes when the post tree holds only the real balances. With
    an unrelated balance under `MIXED` that covers the gap, the manifest check passes, the holder
    whose amount is not in the shared leaf gets `POST_AMOUNT_MISMATCH` and the auditor
    `TARGET_NOT_UNIQUE`; with two equal amounts and a made-up leaf, only the auditor notices
    (`TARGET_NOT_UNIQUE`), under either scope. A creditor converted on the receivables side
    (published liabilities pre total 175000 instead of 475000) passes the manifest check and the
    audit, and gets `SIDE_MISMATCH` with `expected_side="liabilities"`.
14. A record with amount 0: no post proof, and a bundle that carries one gives
    `POST_PROOF_UNEXPECTED`.
15. A seeded mutation test over manifest, bundle and export (at least 4,000, 4,000 and 1,000
    changes): no exception; no changed bundle or export is accepted; a changed manifest is never
    accepted under the published hash.

## 9. The rest of stage A

### 9.1 Big-integer storage and migration (D16)

**Problem.** `entries.amount_cents` is an SQLite `INTEGER`, a signed 64-bit value. Ten units of
an 18-decimal currency do not fit (M2 d). Binding the text of a larger number into that column
does not help: SQLite turns it into a floating-point value without any error (M6, S12).

**Decision.**

- Schema version 1: the column becomes `amount TEXT NOT NULL` holding the canonical decimal text
  of section 4.1, written and read only through `amount_to_text` and `amount_from_text`. A
  non-canonical value read from a file raises `IntegrityError`. As a second line of defence the
  column has this check:
  `CHECK (typeof(amount) = 'text' AND amount NOT GLOB '*[^0-9-]*' AND (amount = '0' OR amount
  GLOB '[1-9]*' OR amount GLOB '-[1-9]*') AND substr(amount, 2) NOT GLOB '*-*' AND
  length(ltrim(amount, '-')) <= 38 AND instr(amount, char(0)) = 0)`.
  The last clause is needed because `length` counts characters only up to the first NUL
  character (S12), and `GLOB` was measured to stop there as well: without it, a text such as
  `1` followed by a NUL and `0` passes the other clauses. `instr` with `char(0)`
  is the way the SQLite documentation gives to find an embedded NUL; it was measured to give
  the same result in UTF-8 and UTF-16 files, whereas comparing `length(CAST(amount AS BLOB))`
  with `length(amount)` refuses every amount in a UTF-16 file. A test compares the check with
  `amount_from_text` on several thousand texts, NUL included, in all three encodings.
- Timestamps are written as the canonical UTC text of section 4.3.
- The file's schema version is `PRAGMA user_version` (S12): 0 for files of 0.1.x, 1 for the new
  schema. A new or empty file is created at version 1. Opening a version 0 file that has data
  raises `SchemaVersionError` (a subclass of `IntegrityError`) that names the migration
  function; opening a file with a higher version than the library knows also raises it. Nothing
  is migrated silently.
- Migration is an explicit call:

  ```
  migrate_ledger_database(path, *, backup_path, applied_at, naive_timestamps="refuse") -> dict
  ```

  It refuses anything that is not a version 0 ledger file, and refuses an existing
  `backup_path`. It first writes a complete copy with SQLite's online backup. Then, in one
  transaction and following the documented procedure for changing a table (S12: foreign keys
  off; build the new table; copy; drop the old one; rename; recreate the two indexes; run
  `foreign_key_check`): every old amount must have the storage class `integer` (otherwise the
  migration stops and lists the entries); each is written as text; row counts and the Python sum
  per account must be equal before and after; every timestamp of the accounts and transactions
  tables is rewritten in canonical UTC text; a row in a new table `schema_migrations(version,
  applied_at, details)` records the step, with `applied_at` taken from the argument (no wall
  clock); `user_version` becomes 1; commit. Any failure rolls everything back, the version
  number included (M6). The backup is not part of that transaction and is not removed: after
  a migration that stopped on the content of the file, the backup is a complete copy of the
  unchanged file and the error message names it, so a second run needs another `backup_path`
  or that file removed first. The errors raised before the backup is written leave no file
  there.
- A stored timestamp without an offset cannot be interpreted safely. With the default
  `naive_timestamps="refuse"` the migration stops and reports it; `"assume-utc"` is the explicit
  statement that such values mean UTC.
- The audit log is a separate store and is not touched: its event hashes are computed over the
  timestamp text as recorded.
- There is no downgrade: 0.1.x cannot read a migrated file (M15), which is why the backup is
  mandatory.

Alternatives. A BLOB with a fixed-width integer: not readable with ordinary tools, and nothing
gains from binary once SQL does no arithmetic. Keeping the `INTEGER` column and adding a text
column only for large values: two representations of one fact. Automatic migration on open: a
silent rewrite of an append-only file.

Tests.

- Test (d) passes as written and its marker is removed.
- Round trip through the store of 0, plus and minus 1, plus and minus (2^63 - 1), plus and minus
  2^63, 10^30 and plus and minus (10^38 - 1); 10^38 is refused before anything is written.
- The balance of 1,000 entries of 18-decimal amounts equals the exact Python sum.
- Writing a row by hand with `-0`, `01`, `1.0`, ` 1` or a 39-digit value violates the column
  check; a row by hand with the integer 5 is stored as the text `5`.
- Migration, on a file produced with the 0.1.x schema inside the test (built from the old
  `CREATE TABLE` statements kept as a fixture): amounts and balances are equal before and after;
  a settlement stored as `2026-01-01T09:00:00+09:00` is counted at the cut-off 2026-01-01T00:00Z
  afterwards; an amount of 10^19 can be posted afterwards; the backup file equals the original
  content; a second run is refused; an existing backup path is refused; a row with a REAL amount
  stops the migration and leaves the file unchanged; a naive timestamp stops it unless
  `assume-utc` is given; `user_version` is 0 after a failed run and 1 after a good one.
- Opening a version 0 file with data raises `SchemaVersionError`; a new file has version 1.
- A conversion of 18-decimal amounts above 2^63 through the ledger (section 8) gives the exact
  values of the vector file.

### 9.2 Coverage as an exact value (D17)

**Problem.** `coverage_ratio` is a `float`, the summary publishes `int(ratio * 10000)`, which is
wrong for 42 of the 40000 small pairs tried (M5), and "no liabilities" is the invented number
999.99.

**Decision.** The exact value is the pair of totals, which the artifact already states. Derived
values are computed with integers:

- `covered` is `reserves >= liabilities`;
- `basis_points_floor` is `(10000 * reserves) // liabilities` (floor division, so it also rounds
  toward minus infinity for a negative reserve total), and `null` when liabilities are 0;
- in Python the ratio is `Fraction(reserves, liabilities)`, or `None` when liabilities are 0.

New per-currency artifact (module `openreserve/transparency/coverage.py`):

```
{ "format": "openreserve.reserve-coverage/1",
  "snapshot": { ...the liabilities snapshot summary of section 7... },
  "reserves_self_booked": { "total": "1000000", "account_ids": ["reserve"] },      ids sorted
  "coverage": { "covered": true, "basis_points_floor": "21052" } }
```

The reserve figure is the signed sum of the settled balances of the operator's reserve accounts
(type `RESERVE`) in that currency at the instant. It is named `reserves_self_booked` because it
is exactly that: what the operator booked, not evidence that assets exist (D18). Receivables are
not counted.

Rounding per account can turn the verdict by itself. Converting a reserve balance of 15 and
three customer balances of 5 at the rate `9` into a unit with two minor units gives reserves
167 (166.67 rounded) and liabilities 168 (three times 55.56 rounded up to 56): covered before
(10000 basis points), not covered after (9940), with nothing else changed. The rounding
difference goes to the institution's residue account (section 8.4), which is not a reserve
account. Coverage after a conversion is a statement about the rounded balances. The
reconciliation figures of the manifest (section 8.4) show how far rounding moved the customer
totals; the reserve accounts are not part of the conversion evidence.

**Check of a published summary.** `verify_reserve_coverage_summary(summary) -> Verification`
takes either format. For `openreserve.reserve-coverage/1` it requires the canonical value
profile and exact key sets, a valid embedded snapshot summary (the rules of section 7), a
signed reserve total within the bound with sorted, unique account ids, a boolean `covered`
and `basis_points_floor` as canonical decimal text or `null` (`MALFORMED_SUMMARY` otherwise);
then `covered` and `basis_points_floor` must be what the rule above gives for the reserve total
and the snapshot's liabilities total (`COVERAGE_MISMATCH`). For
`openreserve.declared-reserve-coverage/1` every currency passes that check (its reason is passed
on), names the institution and instant of the aggregate, and the currencies are sorted and
unique (`MALFORMED_SUMMARY`); `all_covered` must be true exactly when every currency is covered
(`ALL_COVERED_MISMATCH`). Any other document gives `UNSUPPORTED_FORMAT`; no content makes the
function raise. A successful check shows only that the verdicts follow from the totals the
summary states. The reserve total is the operator's own statement, and the liabilities total is
bound to the snapshot root, which holders check with `verify_snapshot_inclusion`.

The legacy `ProofOfReserves` gets the same arithmetic: `coverage_ratio` becomes
`Fraction | None`, and `coverage_ratio_basis_points` in its summary becomes the exact floor or
`null`. The sentinel is gone. This changes legacy output where it was wrong and for the
no-liabilities case.

Tests (`tests/vectors/coverage_v1.json`, 11 pairs): reserves 1, liabilities 3 gives 3333 and
`Fraction(1, 3)`; 57 and 100 gives 5700 (5699 before); 5 and 2 gives 25000; 10^30 + 1 and 10^30
gives 10000; -1 and 3 gives -3334; 5 and 0, and 0 and 0, give `null` and `covered` true. No
float occurs anywhere in a new summary (recursive check), and every amount survives
`json.loads(text, parse_int=float, parse_float=float)` because it is a string. For the fixed-id
ledger of section 7 with a reserve account `reserve` holding 1000000, the coverage summary hash
is `a15cf45f2e16472155dd814c05ac535cf353bf293368fffe0d4ca3112d0eb678`. For the overdraft ledger
with reserves 50: `covered` false, 5000 basis points. Genuine summaries of both formats pass
`verify_reserve_coverage_summary`; with reserves 50 and liabilities 160, a summary whose
`covered` is set to true while the totals stay gives `COVERAGE_MISMATCH`, and an aggregate whose
`all_covered` contradicts its currencies gives `ALL_COVERED_MISMATCH`.

### 9.3 Names (D9)

**Decision.** New public names do not say "solvency" or "solvent", because the check is a
comparison of two booked totals:

| New name | Role | Replaces |
|---|---|---|
| `ReserveCoverage`, `ReserveCoverageGenerator` | per currency: liabilities snapshot, booked reserves, coverage | `ProofOfReserves`, `ProofOfReservesGenerator` |
| `DeclaredReserveCoverage`, `DeclaredReserveCoverageGenerator` | several currencies plus the audit-chain commitment | `ProofOfSolvency`, `ProofOfSolvencyGenerator` |
| `is_covered`, `all_covered` | attributes | `is_solvent`, `overall_solvent` |
| `MerkleProof.verify(expected_root=...)` | legacy inclusion check | `verify_proof_with_node_prefix` |

`ReserveCoverageGenerator(ledger, *, institution_id, salt_key).generate(currency, snapshot_at)`
returns a `ReserveCoverage` with `.snapshot`, `.reserves_total`, `.reserve_account_ids`,
`.is_covered`, `.coverage_ratio`, `.coverage_basis_points_floor`, `.to_public_summary()` and
`.summary_hash()`. `DeclaredReserveCoverageGenerator(...).generate(currencies,
audit_log_root_hash, snapshot_at)` returns per-currency results sorted by currency code, with
`all_covered`; it refuses an empty list of currencies (the legacy aggregate reports "all
solvent" for none). Its summary:
`{"format": "openreserve.declared-reserve-coverage/1", "institution_id", "snapshot_at",
"currencies": [...], "all_covered", "audit_log_committed"}`.

**Deprecation period.** The old names keep working with their old call signatures and their
legacy artifact, with the corrections of sections 4.3, 7 and 9.2. Calling the legacy generators
or `verify_proof_with_node_prefix` emits a `PendingDeprecationWarning` that names the
replacement. It becomes a `DeprecationWarning` in the release that changes the README and the
guides to the new names, and the old names are removed no earlier than two minor releases after
that. Until the README changes, its own example runs the legacy path; that is why the warning is
only the pending kind for now.

The README sentence that defines proof of reserves as evidence that reserve assets exist, and the
phrase "Proof of Solvency", are addressed by the wording proposal of section 13. The code change
does not depend on it.

Tests: each new name is importable from `openreserve`; each old name still imports, runs and, for
the three callables above, warns; the new summaries contain no key or value with "solvent" or
"solvency"; the README example still runs unchanged; an empty currency list is refused by the
new aggregate.

### 9.4 A minimal currency registry (D15)

**Decision.** Stage A adds only what section 8 needs: a description of a currency that is plain
data, and an immutable, hash-identified list of such descriptions.

- `CurrencySpec(code, minor_units)` with the property `fictional`. Rules: the code matches
  `[A-Z][A-Z0-9_]{0,31}`; a code that starts with `FICTIONAL_` is a fictional entry and must have
  at least one more character; only fictional entries may use that prefix; `minor_units` is 0 to
  30. The rule is purely syntactic. It makes a fictional entry visible in every artifact that
  prints the code, and it does not rely on any property of real code lists.
- `CurrencyRegistry`: an immutable collection with `get(code)`, `extended(*specs)` returning a
  new registry (a code that is already present is refused, also when the entry is identical),
  `to_document()` and `registry_hash`. Document:
  `{"format": "openreserve.currency-registry/1", "currencies": [{"code", "fictional",
  "minor_units"}, ...]}` sorted by code, in the canonical profile.
- `DEFAULT_REGISTRY` holds exactly today's eight currencies and no fictional entry. Its hash is
  `a6c8eb8df071e723925179a222c3341d5a0b687020b3c79746d20fc580bf823f`. A test compares it with
  the `Currency` enumeration entry by entry.
- The scenario's W exists only in tests and examples, as `CurrencySpec("FICTIONAL_W", 2)`. The
  default registry extended by it has the hash
  `2ab8443ffc04d3f6e651c6e7c5a6cd22f8a5b2d6293c482344219ba353dc87c2`.
- The ledger keeps its closed `Currency` enumeration in stage A. A registry entry cannot yet be
  used for accounts; conversion arithmetic and evidence work on codes and minor units and
  therefore already accept `FICTIONAL_W`, while ledger-level tests use two existing currencies as
  stand-ins. Opening the ledger to registry entries, effective dates, successor entries and
  pinning a registry hash in ledger files are stage B (R3).

Tests (`tests/vectors/currency_registry_v1.json`): the two hashes; 7 valid and 11 invalid codes;
extension returns a new object and leaves the old hash unchanged; redefinition is refused; a
fictional flag cannot be set on a code without the prefix, and the bare prefix is refused.

### 9.5 Versions and compatibility (E2)

- Every new artifact has a `format` string ending in `/1`. A verifier refuses a format it does
  not know. An artifact without a `format` key is the legacy format of 0.1.x.
- **Old artifacts stay verifiable.** The legacy tree and proof classes are unchanged, and the
  golden vectors of section 5 pin that. A legacy proof published with 0.1.x verifies with
  `MerkleProof.verify(expected_root=...)`.
- Every legacy summary produced by the new code has two more keys, `user_receivables_count`
  and `user_receivables_total` (both 0 when no customer balance is negative). Apart from them,
  legacy summaries differ from 0.1.x in exactly these cases: balances at a cut-off that was
  given, or stored, with a non-UTC offset (now correct); ledgers with a negative customer
  balance (no longer netted); the coverage basis points where the float was wrong; the
  no-liabilities case (`null` instead of 9999900).
- Breaking changes for callers: `fx_convert` signature; naive datetimes refused; `hash_pair`
  removed; `coverage_ratio` type; files need the migration of 9.1.
- The first release that contains these changes is **0.2.0**, not 0.1.2. The version number in
  the package is changed in the release commit, not in this change set.
- Roots of the intent and execution artifacts do not change in stage A.

### 9.6 Tests file, documents, changelog (E3)

- `tests/test_hypothetical_defects.py` keeps its name. Tests (a), (c) and (e) are adapted to the
  new interfaces as described in sections 6.9, 8.8 and 6.9; (b) and (d) pass as written; all five
  markers are removed. The two defects measured later (M3, M4) are first added as strict
  expected failures and then fixed. One strict expected failure remains on purpose: the legacy
  tree's shared root for different totals (section 5, point 6).
- New tests go into files per subject (`test_exact.py`, `test_sumtree.py`, `test_snapshot.py`,
  `test_conversion.py`, `test_conversion_evidence.py`, `test_storage_migration.py`,
  `test_coverage.py`, `test_currency_registry.py`), with the vectors under `tests/vectors/` and
  the standalone recomputation script next to them. A test runs that script.
- The static guard of `tests/test_superrich_guards.py` (no call named `now` or `total_seconds`)
  is extended to the new modules, and a second guard forbids in them: calls named `today`,
  `utcnow`, `urandom`, `uuid4`, `token_bytes`, the built-in `float`, and imports of `random`,
  `secrets`, `time`, `uuid`, `socket`, `ssl`, `http`, `urllib`.
- This document is `docs/hypothetical/W_CHANGEOVER_DESIGN.md`, and the tests file refers to it.
- `CHANGELOG.md` gets the entries under "Unreleased": Added, Changed (with the breaking changes
  of 9.5), Fixed, Deprecated.
- The README, `docs/VERIFYING.md`, `docs/ARCHITECTURE.md`, `examples/` and `examples/README.md`
  are not changed in stage A (section 13).

## 10. Decisions for stage B

These are decided now so that stage A does not have to be redone, but they are not built in
stage A. Their formats get their byte-level detail when they are built.

### 10.1 Reserve evidence from outside the ledger (R5, D18)

- **Status vocabulary.** For each reserve account and snapshot exactly one of: `SELF_BOOKED`
  (nothing but the operator's own entries); `EXTERNAL_SUPPLIED` (an evidence record was supplied;
  when the document itself is given, its SHA-256 was compared with the recorded one);
  `EXTERNAL_SIGNATURE_CHECKED` (in addition, a signature over the record's canonical bytes was
  checked against a key that the caller pinned). The words "verified", "proven" and "attested"
  are not used for reserves.
- **Signature checks.** The standard library has no public-key signature verification, and the
  package keeps having no runtime dependency. The core therefore never checks a public-key
  signature. The third status can only be set through a verifier that the caller passes in (a
  callable taking a key id, message bytes and signature bytes); the artifact records which
  verifier and key fingerprint were used. A shared-secret code is not a signature a third party
  can check and does not count.
- **Cut-off alignment.** No tolerance in the core: an evidence record is `cutoff_aligned` only
  when its statement instant equals the snapshot instant; otherwise the signed difference in
  microseconds is reported as an integer string, and the reader applies their own policy.
- **Headline figure.** The figure built from the operator's own entries stays, always under the
  name `reserves_self_booked` (already so in stage A). A second figure,
  `reserves_externally_evidenced`, sums the asserted amounts of aligned records with one of the
  two external statuses, and has its own coverage values. The two are never added.
- An evidence record holds: supplier, statement instant, a redacted account reference, asserted
  amount and currency, encumbrance notes, document hash, optional detached signature. The library
  never judges whether a document is genuine.

Tests then: a ledger with only own entries never yields an external status or wording; a
document hash mismatch is detected; a different statement instant is flagged with its exact
difference; the two coverage figures are exact and separate.

### 10.2 Publication manifests and witnesses (R6, D19)

- **Canonicalization:** the profile of section 4.2.
- **Chain:** a publication manifest lists, per currency, the snapshot or coverage summary hash,
  plus the registry hash, the audit-chain hash and the hash of the previous manifest of the same
  institution. Two manifests for the same institution and snapshot instant with different hashes
  are an equivocation; the evidence format is `{"format":
  "openreserve.equivocation-evidence/1", "manifest_a", "manifest_b"}` and its check needs no
  secret.
- **Signatures:** detached objects over a manifest hash (`manifest_hash`, `algorithm`, `key_id`,
  `signature`), checked only by optional plug-ins. The core reports "signature present, not
  checked by the core". When a plug-in is used, Ed25519 is the recommended algorithm. No
  dependency is added to the core for it.
- **Witness receipts:** opaque records (`witness_id`, `manifest_hash`, `received_at`, `scheme`,
  receipt bytes), checked by plug-ins. No single external system is required, because any of
  them could be unavailable in the scenario.
- **No network code in the core.** A test scans the package for imports of network modules.

### 10.3 Identifiers (R7, the rest of D22)

Stage A already removes the dependence of roots on creation time and derives conversion
transaction ids from content. Stage B: exports get content-addressed ids next to the stored ones
(the stored random ids stay): the hash of the canonical record plus an occurrence number, so that
two identical legitimate transactions stay distinct; accounts get an explicit opening instant and
an occurrence number. Two independent rebuilds of one event sequence must then give
byte-identical exports. This must not be released before the leaf salts of stage A, which it
follows.

### 10.4 Jurisdiction rules as plug-ins (R8, D23)

The Japan-specific module moves to `openreserve.jurisdictions.jp.payment_services_act`. The old
import path and the top-level names keep working for a deprecation period of two minor releases.
Every output carries `"rule_set": {"id": "jp.payment_services_act", "revision": "<integer>"}`;
the revision increases whenever a calculation changes. The bundled model states no date of a
legal text, because it does not claim to track one. It is described as a rule model, not as a
certification. The core must import and work without any plug-in.

### 10.5 Other stage B items

| Item | Decision or direction |
|---|---|
| Privacy beyond section 6.7 (R10) | Split each balance into several leaves with derived shares and one proof per share; the leaf already has the fields for it (several slots per account become `slot_id` plus a part number in a new format version). Key handling guidance: one key per snapshot series, rotation, storage outside the ledger file |
| Holder-recognisable identifiers | Let the operator bind a reference that the holder already knows (for example a customer number) into the slot id, so that two holders cannot be shown one leaf (section 0, point 5; S3) |
| Intent and execution commitments | Move them to a count-bound tree and remove the last expected failure (section 5). Their roots change, under a format string |
| Open registry in the ledger (R3) | Accounts in registry currencies; effective dates; successor entries that reference a declaration hash; a registry hash pinned in the ledger file |
| Ledger state rules (M10) | Refuse a second reversal of the same transaction and the reversal of a transaction that is not settled; refuse a settlement earlier than the start; record status changes with their instants so that "pending at T" can be reconstructed; a lock on closed periods so that a backdated settlement before a published cut-off is refused |
| Pending amounts at a cut-off | Publish count and total of transactions pending at the instant once status history exists |
| Remaining wall-clock defaults (D20) | Explicit instants for opening an account, settling and building a transaction, with a deprecation period for the defaults |
| Rounding rules | A rounding quantum other than one minor unit; aggregation per holder or per contract; a declared bearer other than the institution |
| Conversions that do not close | Partial conversions of a currency, and conversion into accounts that already hold a balance |
| Examples and guides | New examples for the snapshot, the coverage artifact and a conversion; guides rewritten for the new names (section 13) |
| Performance | Batched balance queries for snapshot generation; measurements on large ledgers |

## 11. Stages

| Stage | Contents |
|---|---|
| **A, calculation and proof core** | R9 one verifier (section 5). R4 exact codecs (4.1, 4.2). D20 instants and E1 cut-off rules (4.3). R1 the sum tree with D1, D2, D3, D5, D6 and the leaf order (6). D4 the liabilities snapshot and the end of netting in the legacy generator (7). R2 conversion: declaration, arithmetic, posting, closing, evidence and the three checks, D10 to D14, and the repaired `fx_convert` (8). E4 the two late defect tests |
| **A, the rest** | D16 text storage and migration (9.1). D17 exact coverage and the coverage artifacts (9.2). D9 names and the deprecation period (9.3). D15 the registry (9.4). E2 format strings and compatibility notes (9.5). E3 tests file, guards, this document, changelog (9.6) |
| **B** | Everything in section 10: reserve evidence from outside (R5), publication and witnesses (R6), content-addressed export ids (R7), jurisdiction plug-ins (R8), the rest of R10 (balance splitting, key handling) and of R3 (registry in the ledger), ledger state rules, the move of the intent and execution commitments, guides and examples |

Order inside the core, each step with its tests before the next: (1) the two late defect tests as
strict expected failures; (2) exact codecs; (3) instants in the store and the ledger; (4) the
legacy verifier; (5) the sum tree; (6) the snapshot and the legacy generator; (7) declaration
and arithmetic; (8) `fx_convert`; (9) posting and closing; (10) evidence and checks.

Expected state of the suite: after the core, no failure and exactly two expected failures
(the storage overflow (d), which the storage change removes, and the legacy tree's shared root);
after the rest of stage A, no failure and exactly one expected failure (the legacy tree's shared
root, a stage B item).

## 12. Tests: vectors, markers, expected counts

The tests of each decision are listed in its section (4.1, 4.2, 4.3, 5, 6.9, 7, 8.8, 9.1 to
9.4). This section is the index.

**Vector files** (`tests/vectors/`). Every expected value in them was computed at design time
with integer and `fractions.Fraction` arithmetic and SHA-256, by code that is not part of the
package, and the tree and conversion values were recomputed by a second, separately written
script.

| File | Pins |
|---|---|
| `exact_v1.json` | amount text (valid, invalid), 16-byte encodings, instant texts, canonical JSON bytes and hashes |
| `sumtree_v1.json` | context digest, salts, leaf hashes, order, top hash, head and every inclusion proof of nine trees; the input bytes of one example of each hash; six context variants and a second key |
| `snapshot_expected_v1.json` | heads and summary hashes of the ledger fixtures of section 7; the disclosure example of 6.7; the coverage summary of 9.2 |
| `conversion_v1.json` | rate texts, refused declarations, nine declarations with hashes and factors, 78 conversions with exact z and e, the inverse-rate case, four batch totals |
| `conversion_evidence_v1.json` | three complete examples: manifest with canonical bytes and hash, transaction ids, record payloads, every holder bundle with its expected details, the auditor export |
| `conversion_ledger_fixture_v1.json` | a ledger-level example: accounts, funding, declaration, the entries of each conversion, ids, the closing amount, the expected manifest and its hash |
| `coverage_v1.json` | 11 pairs of totals with verdict, basis points and ratio |
| `currency_registry_v1.json` | default registry document and hash, the extended one, valid and invalid codes |
| `legacy_merkle_v0.json` | six legacy trees with leaf hashes, roots and proofs |
| `check_vectors_standalone.py` | recomputes the tree, conversion and coverage vectors with the standard library only |

**Markers in `tests/test_hypothetical_defects.py`.**

| Test | What happens |
|---|---|
| (a) totals not bound to the root | adapted to the sum tree (6.9 item 2); marker removed |
| (b) `verify()` rejects a genuine proof | passes unchanged; marker removed |
| (c) `fx_convert` cannot be posted | adapted to the new signature, same balance checks plus the clearing balances (8.8 item 7); marker removed |
| (d) storage overflow | passes unchanged after the storage change; marker removed then |
| (e) unsalted leaves | adapted to the snapshot generator (6.9 item 9); marker removed |
| (f) non-UTC cut-off, new | added as a strict expected failure, then fixed (4.3) |
| (g) negative leaf lowers liabilities, new | added as a strict expected failure, then fixed (7) |
| legacy tree, different totals share a root | stays a strict expected failure until stage B (5) |

A marker is removed in the same commit that makes its test pass, and a test body is changed only
where the interface changed, never to assert less.

**Expected counts.** Before: 194 passed, 5 expected failures. After the core: no failure, exactly
two expected failures. After the rest of stage A: no failure, exactly one expected failure. The
number of passing tests grows with the new files and is reported by whoever runs the suite.

## 13. Public text: proposals, not applied

Stage A changes code, tests, this document and the changelog. It does not change the README, the
guides or the examples. The following wording is decided as a proposal; applying it needs the
maintainer's approval, together with the release that contains the features it describes.

1. **README, top.** After the first paragraph:

   > Experimental software for reproducing accounting snapshots and checking declared conversion
   > calculations. This project does not issue currency or tokens, accept deposits, provide
   > settlement or redemption, recommend financial decisions, or certify regulatory compliance.
   >
   > Successful verification establishes the specific integrity and arithmetic checks documented
   > for that artifact. It does not establish completeness of liabilities, existence or
   > availability of assets, enforceability of claims, or institutional solvency.

2. **README, "What it does" and "Proof of Reserves vs Proof of Solvency".** Replace "Proof of
   Solvency" by "declared reserve coverage", and the sentence that defines proof of reserves as
   evidence that reserve assets exist by: "a commitment to customer balances (a Merkle-sum tree
   whose root binds the total) and a comparison with the reserve balances the operator booked;
   it is not evidence that reserve assets exist". Add the six scope statements of section 0.
3. **README, usage example.** Use `ReserveCoverageGenerator` with an institution id and a key,
   print `is_covered`, the totals and the root, and verify one holder's proof with
   `verify_snapshot_inclusion`.
4. **`docs/VERIFYING.md`.** Describe the new check (root given by the verifier, total and count
   bound to it). Remove the sentence that a proof reveals only the holder's own leaf: it is not
   true for the legacy tree when the neighbour's account id is known (M2 e), and for the new tree
   the statement of section 6.7 applies instead. Say that summaries of the new formats carry
   every integer as a string.
5. **`docs/ARCHITECTURE.md`.** Rename the section on the two artifact kinds and list the new
   modules.
6. **Examples.** A snapshot with a debtor, a coverage artifact, and a conversion with holder
   bundle and auditor export, in the fictional scenario with `FICTIONAL_W` at the evidence level
   and stand-in currencies at the ledger level.
7. The hypothetical scenario is mentioned only in this document and in test and example names. If
   the README mentions it at all, it adds: "Conversion examples are hypothetical, not forecasts or
   proposals. 'W' is a fictional scenario identifier."

Until this is applied, the README example keeps running the legacy path, which is why its
deprecation warning is only the pending kind (section 9.3). Point 4 should not wait long: that
sentence in the verification guide is known to be inaccurate.

## 14. Open

Not measured:

- the suite on Python 3.11, 3.12 and 3.13 (the CI matrix); at design time it ran on 3.14.9 only.
  The reference code and the vectors were run on 3.11.17;
- snapshot generation and conversion on a large ledger (one balance query per account; the tree
  itself was measured, M17);
- whether the published package of version 0.1.1 equals the tag `v0.1.1` (the tag and the
  working tree were compared, M12);
- the CI workflow on this change set (it had not run at design time).

Not read: the full paper of Hu, Zhang and Guo (section 2); ISO 4217.

Left for the maintainer to decide: applying the wording of section 13; publishing this change set
at all, and under which version and date; whether the pending deprecation of the legacy names
turns into a deprecation in the same release as the README change.

Known limits that stage A does not remove (each is a stage B item in section 10): a proof
discloses a neighbour's amount; identifiers are chosen by the operator; roots are not published
or witnessed by the library; backdated settlements are possible; the legacy tree used by the
intent and execution commitments still lets two leaf lists share a root; the ledger cannot hold
accounts in a registry currency such as `FICTIONAL_W`.
