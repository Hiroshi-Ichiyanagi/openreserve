"""Conversion evidence: manifest, holder bundle, auditor export and the three checks
(design sections 8.5 and 8.6).

The three examples of tests/vectors/conversion_evidence_v1.json are reproduced through the
assembler from the inputs stored in each example; the ledger fixture is reproduced through
the ledger. ``FICTIONAL_W`` is a fictional code that exists only in tests.
"""

from __future__ import annotations

import copy
import json
import random
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # helpers shared with the other test modules

import pytest

import openreserve.conversion_evidence as evidence_mod
from openreserve.conversion import (
    CONVERSION_METADATA_KEY,
    ConversionError,
    ConversionIncompleteError,
    closing_transaction_id,
    conversion_transaction_id,
    convert,
    fraction_to_document,
    load_declaration,
    round_fraction,
)
from openreserve.conversion_evidence import (
    FORMAT_BUNDLE,
    FORMAT_CONVERSION_EXPORT,
    FORMAT_MANIFEST,
    ConversionEvidence,
    assemble_conversion_evidence,
    audit_conversion_export,
    build_conversion_evidence,
    verify_conversion_bundle,
    verify_conversion_manifest,
)
from openreserve.core.exact import canonical_json, instant_from_text, sha256_hex
from openreserve.core.ledger import TransactionBuilder
from openreserve.core.types import Currency, Money, OwnerType
from openreserve.transparency.snapshot import verify_snapshot_inclusion
from openreserve.transparency.sumtree import audit_sum_tree_export, verify_sum_tree_inclusion
from test_conversion_ledger import (
    CLEARING_JPY,
    CLEARING_USD,
    CUTOFF,
    DECLARATION,
    EFFECTIVE,
    FIXTURE,
    FUNDED_AT,
    RESIDUE,
    PAIRS,
    ConversionLedger,
)
from test_sumtree import _same, mutate

VECTORS = json.loads((Path(__file__).parent / "vectors" / "conversion_evidence_v1.json").read_text(encoding="utf-8"))
EXAMPLES = {e["name"]: e for e in VECTORS["examples"]}
W2, W0, MIXED = "jpy-to-fictional-w2", "jpy-to-fictional-w0-with-a-zero-result", "jpy-to-fictional-w2-mixed-scope"
KEY = bytes(range(32))
OTHER_HASH = "ab" * 32


def assemble(example, **overrides) -> ConversionEvidence:
    arguments = dict(
        institution_id=example["institution_id"],
        salt_key=bytes.fromhex(example["salt_key_hex"]),
        effective_at=instant_from_text(example["effective_at"]),
        accounts=example["accounts"],
        conversions=[(h["source_slot_id"], int(h["signed_source_amount"]), h["target_slot_id"]) for h in example["holders"]],
        other_target_balances=[(o["slot_id"], int(o["signed_amount"])) for o in example["other_target_currency_balances"]],
        residue_account_change=int(example["residue_account_change_input"]),
    )
    arguments.update(overrides)
    return assemble_conversion_evidence(example["manifest"]["declaration"], **arguments)


@pytest.fixture(scope="module")
def w2() -> ConversionEvidence:
    return assemble(EXAMPLES[W2])


#: The holders who owe their balance at the cut-off in the examples of this file; every other
#: holder is owed theirs. A holder knows which of the two they are from their own record.
DEBTORS = frozenset({"acct-dave"})


def side_of(slot: str) -> str:
    return "receivables" if slot in DEBTORS else "liabilities"


def check_bundle(bundle, manifest, slot, manifest_hash=None, side=None):
    return verify_conversion_bundle(
        bundle, manifest,
        expected_manifest_hash=manifest_hash or sha256_hex(canonical_json(manifest)),
        expected_source_slot_id=slot,
        expected_side=side or side_of(slot),
    )


def fr(document) -> Fraction:
    return Fraction(int(document["num"]), int(document["den"]))


# ---------------------------------------------------------------------------
# 11. The three examples
# ---------------------------------------------------------------------------


def test_pinned_manifest_hashes():
    assert EXAMPLES[W2]["manifest_hash"] == "fe594a86cad764efbec91e827646da25850ef76014a96d4749a73598d9678ac0"
    assert EXAMPLES[W0]["manifest_hash"] == "dde3e7c0066f173a56a0530b77bf6e0f0eaf9dded6936f13299d0ab890de5d29"
    assert EXAMPLES[MIXED]["manifest_hash"] == "52f1ff42c2702dc465d087e36d5c7e353ae09860c0ca3cc16afcabc50e9b82ec"
    assert len(VECTORS["examples"]) == 3


@pytest.mark.parametrize("example", VECTORS["examples"], ids=lambda e: e["name"])
def test_example_is_reproduced_from_its_inputs(example):
    evidence = assemble(example)
    manifest = evidence.manifest
    assert manifest == example["manifest"]
    assert canonical_json(manifest).hex() == example["manifest_canonical_utf8_hex"]
    assert evidence.manifest_hash == example["manifest_hash"] == sha256_hex(canonical_json(manifest))
    assert manifest["format"] == FORMAT_MANIFEST
    assert manifest["declaration"]["target"]["code"] == "FICTIONAL_W"

    # anyone: the manifest alone
    result = verify_conversion_manifest(manifest)
    assert result.ok is True and result.reason is None
    assert result.details["manifest_hash"] == example["manifest_hash"]
    assert result.details["scope"] == manifest["post"]["scope"]

    # every holder
    declaration = load_declaration(manifest["declaration"])
    assert evidence.source_slot_ids() == tuple(sorted(b["source_slot_id"] for b in example["holder_bundles"]))
    for item in example["holder_bundles"]:
        slot = item["source_slot_id"]
        bundle = evidence.bundle_for(slot)
        assert bundle == item["bundle"]
        assert bundle["format"] == FORMAT_BUNDLE and bundle["manifest_hash"] == example["manifest_hash"]
        assert bundle["record_proof"]["leaf"]["payload"] == example["record_payloads"][slot]
        result = verify_conversion_bundle(
            bundle, manifest, expected_manifest_hash=example["manifest_hash"], expected_source_slot_id=slot,
            expected_side=item["expected"]["side"],
        )
        assert result.ok is True and result.reason is None
        expected, d = item["expected"], result.details
        assert d["side"] == expected["side"]
        assert d["source_slot_id"] == slot
        assert d["source_amount"] == int(expected["source_amount"])
        assert d["converted_amount"] == int(expected["converted_amount"])
        assert d["exact"] == fr(expected["exact"]) and d["residue"] == fr(expected["residue"])
        assert d["target_slot_id"] == expected["target_slot_id"]
        assert d["manifest_hash"] == example["manifest_hash"]
        assert d["converted_amount"] - d["exact"] == d["residue"] and abs(d["residue"]) <= Fraction(1, 2)
        assert d["converted_amount"] == convert(declaration, d["source_amount"]).converted_amount
        assert (bundle["post_proof"] is None) == (d["converted_amount"] == 0)
        assert conversion_transaction_id(declaration, slot) == example["conversion_transaction_ids"][slot]
    assert closing_transaction_id(declaration) == example["residue_transaction_id"]

    # holders without a balance have no record
    for holder in example["holders"]:
        if holder["signed_source_amount"] == "0":
            with pytest.raises(KeyError):
                evidence.bundle_for(holder["source_slot_id"])

    # the auditor
    export = evidence.auditor_export()
    assert export == example["auditor_export"]
    assert export["format"] == FORMAT_CONVERSION_EXPORT
    audit = audit_conversion_export(export, expected_manifest_hash=example["manifest_hash"])
    assert audit.ok is True and audit.reason is None
    assert audit.details["record_count"] == len(example["holder_bundles"])


def test_design_figures_of_the_first_example(w2):
    m = w2.manifest
    assert m["liabilities"]["pre"]["total"] == "475000" and m["liabilities"]["pre"]["leaf_count"] == "3"
    assert m["liabilities"]["conversion"]["total"] == "322580"
    assert m["liabilities"]["converted_nonzero_count"] == "3"
    assert m["liabilities"]["exact_total"] == {"num": "10000000", "den": "31"}
    assert m["liabilities"]["rounded_exact_total"] == "322581"
    assert m["liabilities"]["reconciliation_difference"] == "-1"
    assert m["liabilities"]["sum_of_residues"] == {"num": "-20", "den": "31"}
    assert m["receivables"]["pre"]["total"] == "60" and m["receivables"]["conversion"]["total"] == "41"
    assert m["post"] == {
        "scope": "CONVERSION_ONLY",
        "liabilities": {"root": m["post"]["liabilities"]["root"], "total": "322580", "leaf_count": "3"},
        "receivables": {"root": m["post"]["receivables"]["root"], "total": "41", "leaf_count": "1"},
    }
    assert m["ledger_closing"] == {"bearer": "INSTITUTION", "residue_account_change": "1"}
    assert m["accounts"] == {"clearing_source": "acct-clearing-jpy", "clearing_target": "acct-clearing-w", "residue": "acct-residue-w"}
    assert m["effective_at"] == "2099-12-31T15:00:01.000000Z"


def test_the_six_trees_have_the_documented_contexts(w2):
    m = w2.manifest
    declaration_hash = m["declaration_hash"]
    expected = {
        ("liabilities", "pre"): ("liabilities", "JPY", 0, "2099-12-31T15:00:00.000000Z", ""),
        ("receivables", "pre"): ("receivables", "JPY", 0, "2099-12-31T15:00:00.000000Z", ""),
        ("liabilities", "conversion"): ("conversion-liabilities", "FICTIONAL_W", 2, "2099-12-31T15:00:01.000000Z", declaration_hash),
        ("receivables", "conversion"): ("conversion-receivables", "FICTIONAL_W", 2, "2099-12-31T15:00:01.000000Z", declaration_hash),
        ("liabilities", "post"): ("liabilities", "FICTIONAL_W", 2, "2099-12-31T15:00:01.000000Z", ""),
        ("receivables", "post"): ("receivables", "FICTIONAL_W", 2, "2099-12-31T15:00:01.000000Z", ""),
    }
    for (side, stage), (kind, currency, minor_units, as_of, binding) in expected.items():
        context = w2.tree(side, stage).context
        assert (context.kind, context.currency, context.minor_units, context.as_of, context.binding.hex()) == (
            kind, currency, minor_units, as_of, binding,
        )
        assert context.institution_id == "example-institution"
        assert w2.tree(side, stage).head() == (m["post"][side] if stage == "post" else m[side][stage])
    # the pre and post trees are ordinary snapshot trees: each proof is also a tree proof
    bundle = w2.bundle_for("acct-alice")
    assert verify_sum_tree_inclusion(
        bundle["pre_proof"], expected_root=m["liabilities"]["pre"]["root"], expected_slot_id="acct-alice"
    ).details["amount"] == 300_000
    assert verify_sum_tree_inclusion(
        bundle["post_proof"], expected_root=m["post"]["liabilities"]["root"], expected_slot_id="acct-alice-w"
    ).details["amount"] == 203_735
    for side in ("liabilities", "receivables"):
        for stage in ("pre", "conversion", "post"):
            tree = w2.tree(side, stage)
            assert audit_sum_tree_export(tree.export(), expected_root=tree.root).ok


def test_manifest_contains_no_salt_no_slot_id_and_no_number(w2):
    text = canonical_json(w2.manifest).decode("utf-8")
    for side in ("liabilities", "receivables"):
        for stage in ("pre", "conversion", "post"):
            for leaf in w2.tree(side, stage).leaves:
                assert leaf.slot_id not in text
                assert leaf.salt.hex() not in text
    assert json.loads(text, parse_int=float, parse_float=float) == w2.manifest  # no JSON number anywhere
    assert "salt" not in text and "payload" not in text


def test_assembly_is_deterministic_and_independent_of_input_order():
    example = EXAMPLES[W2]
    conversions = [(h["source_slot_id"], int(h["signed_source_amount"]), h["target_slot_id"]) for h in example["holders"]]
    a = assemble(example, conversions=conversions)
    b = assemble(example, conversions=list(reversed(conversions)))
    assert a.manifest_hash == b.manifest_hash == example["manifest_hash"]
    assert a.auditor_export() == b.auditor_export()
    other_key = assemble(example, salt_key=bytes(range(1, 33)))
    assert other_key.manifest_hash != a.manifest_hash
    assert other_key.manifest["liabilities"]["pre"]["total"] == "475000"


def test_assembler_computes_amounts_itself_and_refuses_bad_input():
    example = EXAMPLES[W2]
    import inspect

    parameters = inspect.signature(assemble_conversion_evidence).parameters
    assert "converted" not in " ".join(parameters) and "target_amount" not in parameters
    good = [("acct-alice", 300_000, "acct-alice-w")]
    bad_cases = [
        dict(conversions=[("acct-alice", 300_000, "acct-alice-w"), ("acct-alice", 1, "acct-x")]),   # source twice
        dict(conversions=[("acct-alice", 300_000, "acct-w"), ("acct-bob", 1, "acct-w")]),           # target twice
        dict(conversions=[("acct-alice", 300_000, "acct-alice-w", 203_735)]),                       # an amount as input
        dict(conversions=[("acct-alice", 300_000.0, "acct-alice-w")]),
        dict(conversions=[("acct-alice", "300000", "acct-alice-w")]),
        dict(conversions=[("acct-alice", True, "acct-alice-w")]),
        dict(conversions=[("acct-alice", 10**38, "acct-alice-w")]),
        dict(conversions=[("", 1, "acct-alice-w")]),
        dict(conversions=[("acct-alice", 1, "")]),
        dict(conversions=good, other_target_balances=[("acct-alice-w", 5)]),                        # a target listed as other
        dict(conversions=good, other_target_balances=[("acct-o", 5), ("acct-o", 6)]),
        dict(conversions=good, other_target_balances=[("acct-o", 5.0)]),
        dict(conversions=good, other_target_balances=[("acct-o",)]),
        dict(conversions=good, residue_account_change=1.0),
        dict(conversions=good, residue_account_change=True),
        dict(conversions=good, residue_account_change=10**38),
        dict(conversions=good, effective_at=instant_from_text("2099-12-31T15:00:00.000000Z")),      # not after the cut-off
        dict(conversions=good, effective_at=datetime(2100, 1, 1)),                                  # naive
        dict(conversions=good, salt_key=b"short"),
        dict(conversions=good, institution_id=""),
        dict(conversions=good, accounts={"clearing_source": "a", "clearing_target": "b"}),
        dict(conversions=good, accounts={"clearing_source": "a", "clearing_target": "a", "residue": "c"}),
        dict(conversions=good, accounts={"clearing_source": "a", "clearing_target": "b", "residue": ""}),
        dict(conversions=good, accounts=None),
    ]
    for case in bad_cases:
        with pytest.raises(ConversionError):
            assemble(example, **case)
    assert assemble(example, conversions=good).manifest["liabilities"]["conversion"]["total"] == "203735"
    # a target listed twice is refused also when one of the two amounts converts to 0 and
    # would therefore leave no second leaf in the post tree
    zero_example = EXAMPLES[W0]
    assert convert(zero_example["manifest"]["declaration"], 73).converted_amount == 0
    for conversions in (
        [("acct-zed", 73, "acct-t"), ("acct-yan", 74, "acct-t")],
        [("acct-yan", 74, "acct-t"), ("acct-zed", 73, "acct-t")],
        [("acct-zed", 73, "acct-t"), ("acct-none", 0, "acct-t")],
    ):
        with pytest.raises(ConversionError):
            assemble(zero_example, conversions=conversions)


# ---------------------------------------------------------------------------
# 8 and 10. The ledger fixture
# ---------------------------------------------------------------------------


def build(f: ConversionLedger, **overrides) -> ConversionEvidence:
    arguments = dict(
        institution_id=FIXTURE["institution_id"],
        salt_key=bytes.fromhex(FIXTURE["salt_key_hex"]),
        clearing_source_account_id=CLEARING_JPY,
        clearing_target_account_id=CLEARING_USD,
        residue_account_id=RESIDUE,
        effective_at=EFFECTIVE,
    )
    arguments.update(overrides)
    return build_conversion_evidence(f.ledger, DECLARATION, **arguments)


def test_ledger_fixture_gives_the_pinned_manifest_and_building_twice_is_stable():
    f = ConversionLedger()
    f.convert_all()
    assert f.close() == 1
    evidence = build(f)
    assert evidence.manifest == FIXTURE["expected_manifest"]
    assert evidence.manifest_hash == FIXTURE["expected_manifest_hash"] == "aced3b5d7dba19e83eb8b1e8fba2b85441864dacbf53c501d22d6c91fb5067e4"
    again = build(f)
    assert again.manifest_hash == evidence.manifest_hash
    assert again.auditor_export() == evidence.auditor_export()

    m = evidence.manifest
    assert m["liabilities"]["conversion"]["total"] == "5277777" and m["liabilities"]["rounded_exact_total"] == "5277778"
    assert m["receivables"]["conversion"]["total"] == "667"
    assert m["post"]["scope"] == "CONVERSION_ONLY"
    assert m["ledger_closing"]["residue_account_change"] == "1"
    assert verify_conversion_manifest(m).ok

    # only customer accounts have records; the platform and reserve conversions are not in the evidence
    assert evidence.source_slot_ids() == ("acct-alice", "acct-bob", "acct-carol", "acct-dave")
    expected = {"acct-alice": ("liabilities", 300_000, 3_333_333, "acct-alice-usd"),
                "acct-bob": ("liabilities", 120_000, 1_333_333, "acct-bob-usd"),
                "acct-carol": ("liabilities", 55_000, 611_111, "acct-carol-usd"),
                "acct-dave": ("receivables", 60, 667, "acct-dave-usd")}
    for slot, (side, x, y, target) in expected.items():
        result = check_bundle(evidence.bundle_for(slot), m, slot, FIXTURE["expected_manifest_hash"])
        assert result.ok, result
        assert (result.details["side"], result.details["source_amount"], result.details["converted_amount"],
                result.details["target_slot_id"]) == (side, x, y, target)
    for slot in ("acct-erin", "acct-platform-jpy", "acct-reserve-jpy", "acct-nobody"):
        with pytest.raises(KeyError):
            evidence.bundle_for(slot)
    assert audit_conversion_export(evidence.auditor_export(), expected_manifest_hash=evidence.manifest_hash).ok

    # the post trees are the ordinary liabilities snapshot of the target currency at that instant
    from openreserve.transparency.snapshot import LiabilitiesSnapshotGenerator

    snapshot = LiabilitiesSnapshotGenerator(
        f.ledger, institution_id=FIXTURE["institution_id"], salt_key=bytes.fromhex(FIXTURE["salt_key_hex"])
    ).generate(Currency.USD, EFFECTIVE)
    assert snapshot.to_public_summary()["liabilities"] == m["post"]["liabilities"]
    assert snapshot.to_public_summary()["receivables"] == m["post"]["receivables"]
    assert verify_snapshot_inclusion(
        evidence.bundle_for("acct-alice")["post_proof"], snapshot.to_public_summary(), expected_slot_id="acct-alice-usd",
        expected_kind="liabilities",
    ).ok
    # and the pre trees the snapshot of the source currency at the cut-off
    pre = LiabilitiesSnapshotGenerator(
        f.ledger, institution_id=FIXTURE["institution_id"], salt_key=bytes.fromhex(FIXTURE["salt_key_hex"])
    ).generate(Currency.JPY, CUTOFF)
    assert pre.to_public_summary()["liabilities"] == m["liabilities"]["pre"]
    assert pre.to_public_summary()["receivables"] == m["receivables"]["pre"]
    f.storage.close()


def test_evidence_before_closing_or_before_every_conversion_is_incomplete():
    f = ConversionLedger()
    with pytest.raises(ConversionIncompleteError):
        build(f)  # nothing converted
    f.convert_all(pairs=[p for p in FIXTURE["conversion_pairs"] if p[0] != "acct-carol"])
    with pytest.raises(ConversionIncompleteError):
        build(f)  # carol is not converted
    f.convert("acct-carol", "acct-carol-usd")
    with pytest.raises(ConversionIncompleteError):
        build(f)  # converted, but not closed
    f.close()
    assert build(f).manifest_hash == FIXTURE["expected_manifest_hash"]
    f.storage.close()


def test_customer_emptied_without_a_conversion_is_not_silently_left_out_of_the_evidence():
    # carol's balance at the cut-off is moved to the source clearing account by a plain
    # transfer instead of a conversion. Every balance of the source currency is then zero and
    # the closing goes through, but carol has no conversion: the evidence must not be built
    # without her.
    f = ConversionLedger()
    f.move("acct-carol", CLEARING_JPY, 55_000, at=EFFECTIVE)
    f.convert_all(pairs=[p for p in FIXTURE["conversion_pairs"] if p[0] != "acct-carol"])
    assert f.balance("acct-carol") == 0 and f.balance("acct-carol", CUTOFF) == 55_000
    assert f.balance(CLEARING_JPY) == 0
    f.close()
    with pytest.raises(ConversionIncompleteError):
        build(f)
    f.storage.close()


def _post_late(f: ConversionLedger, src: str, dst: str, amount: int, *, settled_at: datetime) -> None:
    """A transfer entered after the effective instant and settled with an earlier instant."""
    b = TransactionBuilder("MOVE", "test", initiated_at=EFFECTIVE + timedelta(days=1))
    b.transfer(src, dst, Money(cents=amount, currency=Currency.JPY))
    f.ledger.post(b.build())
    f.ledger.settle(b.transaction_id, settled_at=settled_at)


def test_balance_at_the_cutoff_changed_after_the_conversion_is_refused():
    # After alice's conversion of 300000, a backdated +10 is settled before the cut-off and a
    # compensating -10 between the cut-off and the effective instant. Her balance at the
    # effective instant is still 0 and the closing still holds, but her balance at the cut-off
    # is now 300010, which is not the amount that was converted.
    f = ConversionLedger()
    f.convert_all()
    f.close()
    _post_late(f, "acct-platform-jpy", "acct-alice", 10, settled_at=CUTOFF - timedelta(hours=1))
    _post_late(f, "acct-alice", "acct-platform-jpy", 10, settled_at=CUTOFF + timedelta(microseconds=500_000))
    assert f.balance("acct-alice", CUTOFF) == 300_010
    assert f.balance("acct-alice", EFFECTIVE) == 0 and f.balance("acct-platform-jpy", EFFECTIVE) == 0
    assert f.balance("acct-alice-usd", EFFECTIVE) == 3_333_333
    with pytest.raises(ConversionIncompleteError, match="acct-alice"):
        build(f)
    f.storage.close()


def test_conversion_of_an_account_whose_balance_at_the_cutoff_became_zero_is_refused():
    # The same kind of late correction, but it takes alice's balance at the cut-off to 0: she
    # has a conversion transaction and no balance to convert.
    f = ConversionLedger()
    f.convert_all()
    f.close()
    _post_late(f, "acct-alice", "acct-platform-jpy", 300_000, settled_at=CUTOFF - timedelta(hours=1))
    _post_late(f, "acct-platform-jpy", "acct-alice", 300_000, settled_at=CUTOFF + timedelta(microseconds=500_000))
    assert f.balance("acct-alice", CUTOFF) == 0 and f.balance("acct-alice", EFFECTIVE) == 0
    with pytest.raises(ConversionError, match="has a conversion but no balance at the cut-off"):
        build(f)
    f.storage.close()


def test_conversion_target_that_is_not_a_customer_account_is_refused():
    # post_conversion never books a customer's balance to an account of another type; a
    # transaction made by hand in the documented format and with the derived id must not
    # get such a target into the evidence either.
    scratch = ConversionLedger()
    genuine = scratch.convert("acct-alice", "acct-alice-usd")
    scratch.storage.close()
    f = ConversionLedger()
    f.open("acct-platform-usd-2", OwnerType.PLATFORM, Currency.USD)
    record = genuine.metadata[0][1]
    assert record.count('"target_account_id":"acct-alice-usd"') == 1
    record = record.replace('"target_account_id":"acct-alice-usd"', '"target_account_id":"acct-platform-usd-2"')
    entries = genuine.entries[:3] + (replace(genuine.entries[3], account_id="acct-platform-usd-2"),)
    f.ledger.post(replace(genuine, entries=entries, metadata=((CONVERSION_METADATA_KEY, record),)))
    f.convert_all(pairs=[p for p in PAIRS if p[0] != "acct-alice"])
    f.close()
    assert f.balance("acct-platform-usd-2", EFFECTIVE) == 3_333_333
    with pytest.raises(ConversionError, match="not a customer account of the target currency"):
        build(f)
    f.storage.close()


def test_evidence_from_the_ledger_refuses_wrong_arguments_and_later_changes():
    f = ConversionLedger()
    f.open("acct-residue-usd-2", OwnerType.FX_GAIN_LOSS, Currency.USD)
    f.convert_all()
    f.close()
    for overrides in (
        dict(residue_account_id="acct-residue-usd-2"),   # not the account the closing used
        dict(residue_account_id="acct-platform-usd"),
        dict(clearing_target_account_id=RESIDUE),
        dict(salt_key=b"short"),
        dict(institution_id=""),
        dict(effective_at=CUTOFF),
        dict(effective_at=datetime(2026, 7, 1)),
    ):
        with pytest.raises(ConversionError):
            build(f, **overrides)
    # a later instant gives other evidence (another instant in the contexts), still consistent
    later = build(f, effective_at=EFFECTIVE + timedelta(hours=1))
    assert later.manifest_hash != FIXTURE["expected_manifest_hash"]
    assert verify_conversion_manifest(later.manifest).ok
    # a target that received something else by then no longer holds exactly its converted amount
    f.move("acct-platform-usd", "acct-alice-usd", 5, at=EFFECTIVE + timedelta(minutes=1), currency=Currency.USD)
    with pytest.raises(ConversionError):
        build(f, effective_at=EFFECTIVE + timedelta(hours=1))
    assert build(f).manifest_hash == FIXTURE["expected_manifest_hash"]  # the evidence at the effective instant is unchanged
    f.storage.close()


def test_other_customer_balances_in_the_target_currency_give_scope_mixed():
    f = ConversionLedger()
    f.open("acct-zoe-usd", OwnerType.USER, Currency.USD)
    f.move("acct-platform-usd", "acct-zoe-usd", 500, at=FUNDED_AT, currency=Currency.USD)
    f.convert_all(pairs=[p for p in FIXTURE["conversion_pairs"] if p[0] != "acct-platform-jpy"])
    # the platform's USD account already has entries, so it cannot be a conversion target;
    # a fresh one is used instead
    f.open("acct-platform-usd-2", OwnerType.PLATFORM, Currency.USD)
    f.convert("acct-platform-jpy", "acct-platform-usd-2")
    assert f.close() == 1
    evidence = build(f)
    m = evidence.manifest
    assert m["post"]["scope"] == "MIXED"
    assert m["post"]["liabilities"]["total"] == str(5_277_777 + 500) and m["post"]["liabilities"]["leaf_count"] == "4"
    assert m["liabilities"]["conversion"]["total"] == "5277777"
    assert verify_conversion_manifest(m).ok
    for slot in evidence.source_slot_ids():
        assert check_bundle(evidence.bundle_for(slot), m, slot).ok
    assert audit_conversion_export(evidence.auditor_export(), expected_manifest_hash=evidence.manifest_hash).ok
    f.storage.close()


def test_other_balances_of_zero_leave_the_scope_conversion_only():
    example = EXAMPLES[W2]
    plain = assemble(example)
    assert plain.manifest["post"]["scope"] == "CONVERSION_ONLY"
    with_zeros = assemble(example, other_target_balances=[("acct-zoe-w", 0), ("acct-yuki-w", 0)])
    assert with_zeros.manifest["post"]["scope"] == "CONVERSION_ONLY"
    assert with_zeros.manifest_hash == plain.manifest_hash == example["manifest_hash"]
    # one balance that is not zero is enough for MIXED
    one = assemble(example, other_target_balances=[("acct-zoe-w", 0), ("acct-yuki-w", -1)])
    assert one.manifest["post"]["scope"] == "MIXED"


# ---------------------------------------------------------------------------
# 12. Changes to a genuine bundle: exact reasons
# ---------------------------------------------------------------------------


def _changed(document, fn):
    changed = copy.deepcopy(document)
    fn(changed)
    return changed


def test_changes_to_a_genuine_bundle(w2):
    manifest, manifest_hash = w2.manifest, w2.manifest_hash
    alice, bob, dave = (w2.bundle_for(s) for s in ("acct-alice", "acct-bob", "acct-dave"))

    def reason(bundle, slot="acct-alice", m=manifest, h=manifest_hash):
        return verify_conversion_bundle(
            bundle, m, expected_manifest_hash=h, expected_source_slot_id=slot, expected_side=side_of(slot)
        ).reason

    assert reason(alice) is None
    plus_one = lambda proof: proof["leaf"].__setitem__("amount", str(int(proof["leaf"]["amount"]) + 1))
    assert reason(_changed(alice, lambda b: plus_one(b["post_proof"]))) == "POST_ROOT_MISMATCH"
    assert reason(_changed(alice, lambda b: plus_one(b["record_proof"]))) == "RECORD_ROOT_MISMATCH"
    assert reason(_changed(alice, lambda b: plus_one(b["pre_proof"]))) == "PRE_ROOT_MISMATCH"
    assert reason(bob) == "PRE_SLOT_MISMATCH"                                     # the bundle of another holder
    assert reason(_changed(alice, lambda b: b.__setitem__("post_proof", bob["post_proof"]))) == "POST_SLOT_MISMATCH"
    assert reason(_changed(alice, lambda b: b.__setitem__("post_proof", None))) == "POST_PROOF_MISSING"
    assert reason(_changed(alice, lambda b: b["pre_proof"]["context"].__setitem__("as_of", "2099-12-31T15:00:00.000001Z"))) == "PRE_CONTEXT_MISMATCH"
    assert reason(_changed(alice, lambda b: b["record_proof"]["context"].__setitem__("binding", "11" * 32))) == "RECORD_CONTEXT_MISMATCH"
    assert reason(_changed(alice, lambda b: b.__setitem__("side", "receivables"))) == "PRE_CONTEXT_MISMATCH"
    assert reason(alice, h=OTHER_HASH) == "MANIFEST_HASH_MISMATCH"

    changed_total = _changed(manifest, lambda m: m["liabilities"]["conversion"].__setitem__("total", "322581"))
    assert reason(alice, m=changed_total) == "RESIDUE_MISMATCH"
    assert verify_conversion_manifest(changed_total).reason == "RESIDUE_MISMATCH"
    inverted = _changed(manifest, lambda m: m["declaration"]["rate"].__setitem__("quotation", "TARGET_PER_SOURCE"))
    assert reason(alice, m=inverted) == "DECLARATION_HASH_MISMATCH"
    assert verify_conversion_manifest(inverted).reason == "DECLARATION_HASH_MISMATCH"

    # more of the same kind
    assert reason(_changed(alice, lambda b: b.__setitem__("record_proof", bob["record_proof"]))) == "RECORD_SLOT_MISMATCH"
    assert reason(_changed(alice, lambda b: b.__setitem__("pre_proof", b["post_proof"]))) == "PRE_CONTEXT_MISMATCH"
    assert reason(_changed(alice, lambda b: b.__setitem__("post_proof", b["pre_proof"]))) == "POST_CONTEXT_MISMATCH"
    assert reason(_changed(alice, lambda b: b.__setitem__("record_proof", b["post_proof"]))) == "RECORD_CONTEXT_MISMATCH"
    assert reason(_changed(alice, lambda b: b["post_proof"]["context"].__setitem__("currency", "JPY"))) == "POST_CONTEXT_MISMATCH"
    assert reason(_changed(alice, lambda b: b["record_proof"]["context"].__setitem__("kind", "conversion-receivables"))) == "RECORD_CONTEXT_MISMATCH"
    assert reason(_changed(alice, lambda b: b["record_proof"]["leaf"].__setitem__(
        "payload", '{"source_amount":"300000","target_slot_id":"acct-bob-w"}'))) == "RECORD_ROOT_MISMATCH"
    assert reason(_changed(alice, lambda b: b.__setitem__("manifest_hash", OTHER_HASH))) == "MANIFEST_HASH_MISMATCH"
    assert reason(_changed(alice, lambda b: b["pre_proof"]["path"].pop())) == "PRE_PATH_LENGTH_MISMATCH"
    assert reason(_changed(alice, lambda b: b["post_proof"].__setitem__("format", "x"))) == "POST_UNSUPPORTED_FORMAT"
    assert reason(_changed(alice, lambda b: b.__setitem__("pre_proof", None))) == "PRE_UNSUPPORTED_FORMAT"
    assert reason(_changed(alice, lambda b: b["record_proof"]["leaf"].pop("salt"))) == "RECORD_MALFORMED_PROOF"
    # the debtor's bundle verifies on its own side and nowhere else
    assert reason(dave, slot="acct-dave") is None
    assert reason(_changed(dave, lambda b: b.__setitem__("side", "liabilities")), slot="acct-dave") == "PRE_CONTEXT_MISMATCH"

    for malformed in (
        _changed(alice, lambda b: b.pop("side")),
        _changed(alice, lambda b: b.pop("post_proof")),
        _changed(alice, lambda b: b.__setitem__("extra", "x")),
        _changed(alice, lambda b: b.__setitem__("format", "openreserve.conversion.holder-bundle/2")),
        _changed(alice, lambda b: b.__setitem__("side", "assets")),
        _changed(alice, lambda b: b.__setitem__("side", None)),
        _changed(alice, lambda b: b["pre_proof"]["leaf"].__setitem__("amount", 300000)),  # a JSON number
        None, [], "bundle", {}, manifest,
    ):
        assert reason(malformed) == "MALFORMED_BUNDLE"

    with pytest.raises(ValueError):
        verify_conversion_bundle(alice, manifest, expected_manifest_hash="xyz", expected_source_slot_id="acct-alice",
                                 expected_side="liabilities")
    with pytest.raises(ValueError):
        verify_conversion_bundle(alice, manifest, expected_manifest_hash=manifest_hash, expected_source_slot_id="",
                                 expected_side="liabilities")
    for side in ("assets", "Liabilities", "", None, 0):
        with pytest.raises(ValueError):
            verify_conversion_bundle(alice, manifest, expected_manifest_hash=manifest_hash,
                                     expected_source_slot_id="acct-alice", expected_side=side)


def test_a_creditor_converted_as_a_debtor_is_told_so():
    # The institution owed alice 300000 at the cut-off but converts her balance on the
    # receivables side: the published liabilities totals drop by her amount, the manifest is
    # consistent with itself, the export passes the audit, and her bundle is genuine.
    example = EXAMPLES[W2]
    flipped = [
        (h["source_slot_id"], -int(h["signed_source_amount"]) if h["source_slot_id"] == "acct-alice" else int(h["signed_source_amount"]),
         h["target_slot_id"])
        for h in example["holders"]
    ]
    cheat = assemble(example, conversions=flipped)
    m = cheat.manifest
    assert example["manifest"]["liabilities"]["pre"]["total"] == "475000"
    assert m["liabilities"]["pre"]["total"] == "175000"
    assert verify_conversion_manifest(m).ok
    assert audit_conversion_export(cheat.auditor_export(), expected_manifest_hash=cheat.manifest_hash).ok
    bundle = cheat.bundle_for("acct-alice")

    result = check_bundle(bundle, m, "acct-alice", side="liabilities")
    assert (result.ok, result.reason) == (False, "SIDE_MISMATCH")
    assert result.details["side"] == "receivables"
    # the bundle itself is genuine: it shows a debt of 300000 converted to a debt of 203735
    as_debt = check_bundle(bundle, m, "acct-alice", side="receivables")
    assert as_debt.ok is True
    assert (as_debt.details["side"], as_debt.details["source_amount"], as_debt.details["converted_amount"]) == (
        "receivables", 300_000, 203_735)
    # the honest debtor of the example passes on the side they expect and is told when they expect the other
    dave = assemble(example).bundle_for("acct-dave")
    assert check_bundle(dave, example["manifest"], "acct-dave", side="receivables").ok
    assert check_bundle(dave, example["manifest"], "acct-dave", side="liabilities").reason == "SIDE_MISMATCH"


def test_manifest_reasons(w2):
    manifest = w2.manifest

    def reason(fn):
        return verify_conversion_manifest(_changed(manifest, fn)).reason

    assert verify_conversion_manifest(manifest).reason is None
    # shape and field rules
    for fn in (
        lambda m: m.pop("format"),
        lambda m: m.__setitem__("format", "openreserve.conversion.manifest/2"),
        lambda m: m.__setitem__("extra", "x"),
        lambda m: m.pop("post"),
        lambda m: m.__setitem__("institution_id", ""),
        lambda m: m.__setitem__("declaration_hash", "AB" * 32),
        lambda m: m.__setitem__("effective_at", "2099-12-31T15:00:01Z"),
        lambda m: m["declaration"]["rate"].__setitem__("text", "0"),
        lambda m: m["declaration"]["rounding"].__setitem__("mode", "HALF_UP"),
        lambda m: m["declaration"].pop("cutoff_at"),
        lambda m: m.__setitem__("declaration", None),
        lambda m: m["accounts"].pop("residue"),
        lambda m: m["accounts"].__setitem__("residue", m["accounts"]["clearing_target"]),
        lambda m: m["accounts"].__setitem__("extra", "x"),
        lambda m: m["liabilities"].pop("sum_of_residues"),
        lambda m: m["liabilities"].__setitem__("extra", "x"),
        lambda m: m["liabilities"]["pre"].__setitem__("total", 475000),
        lambda m: m["liabilities"]["pre"].__setitem__("root", "x"),
        lambda m: m["liabilities"].__setitem__("converted_nonzero_count", "-1"),
        lambda m: m["liabilities"]["exact_total"].__setitem__("den", "0"),
        lambda m: m["liabilities"].__setitem__("exact_total", {"num": "20000000", "den": "62"}),  # not in lowest terms
        lambda m: m["liabilities"].__setitem__("exact_total", {"num": "-10000000", "den": "31"}),
        lambda m: m["liabilities"].__setitem__("rounded_exact_total", "-322581"),
        lambda m: m["liabilities"].__setitem__("reconciliation_difference", "+1"),
        lambda m: m["receivables"].__setitem__("sum_of_residues", "149/589"),
        lambda m: m["post"].__setitem__("scope", "ALL"),
        lambda m: m["post"].pop("receivables"),
        lambda m: m["post"]["liabilities"].__setitem__("leaf_count", "03"),
        lambda m: m["ledger_closing"].__setitem__("bearer", "HOLDERS"),
        lambda m: m["ledger_closing"].__setitem__("residue_account_change", 1),
        lambda m: m["ledger_closing"].__setitem__("residue_account_change", "1.0"),
        lambda m: m["ledger_closing"].__setitem__("extra", "x"),
    ):
        assert reason(fn) == "MALFORMED_MANIFEST"
    for document in (None, 5, [], "manifest", {}, w2.bundle_for("acct-alice")):
        assert verify_conversion_manifest(document).reason == "MALFORMED_MANIFEST"

    assert reason(lambda m: m.__setitem__("declaration_hash", OTHER_HASH)) == "DECLARATION_HASH_MISMATCH"
    assert reason(lambda m: m["declaration"]["rate"].__setitem__("text", "147.250")) == "DECLARATION_HASH_MISMATCH"
    assert reason(lambda m: m["declaration"].__setitem__("declaration_id", "another")) == "DECLARATION_HASH_MISMATCH"
    assert reason(lambda m: m.__setitem__("effective_at", m["declaration"]["cutoff_at"])) == "EFFECTIVE_NOT_AFTER_CUTOFF"
    assert reason(lambda m: m.__setitem__("effective_at", "2099-12-31T14:59:59.999999Z")) == "EFFECTIVE_NOT_AFTER_CUTOFF"

    assert reason(lambda m: m["liabilities"]["conversion"].__setitem__("leaf_count", "2")) == "COUNT_MISMATCH"
    assert reason(lambda m: m["liabilities"]["pre"].__setitem__("leaf_count", "4")) == "COUNT_MISMATCH"
    assert reason(lambda m: m["liabilities"].__setitem__("converted_nonzero_count", "4")) == "COUNT_MISMATCH"
    assert reason(lambda m: m["liabilities"]["pre"].__setitem__("total", "475001")) == "EXACT_TOTAL_MISMATCH"
    assert reason(lambda m: m["liabilities"].__setitem__("exact_total", {"num": "322581", "den": "1"})) == "EXACT_TOTAL_MISMATCH"
    assert reason(lambda m: m["liabilities"].__setitem__("rounded_exact_total", "322580")) == "EXACT_TOTAL_MISMATCH"
    assert reason(lambda m: m["liabilities"]["conversion"].__setitem__("total", "322581")) == "RESIDUE_MISMATCH"
    assert reason(lambda m: m["liabilities"].__setitem__("reconciliation_difference", "0")) == "RESIDUE_MISMATCH"
    assert reason(lambda m: m["receivables"].__setitem__("sum_of_residues", {"num": "0", "den": "1"})) == "RESIDUE_MISMATCH"
    assert reason(lambda m: m["post"]["liabilities"].__setitem__("total", "322579")) == "POST_TOTAL_MISMATCH"
    assert reason(lambda m: m["post"]["liabilities"].__setitem__("total", "322581")) == "POST_TOTAL_MISMATCH"
    assert reason(lambda m: m["post"]["liabilities"].__setitem__("leaf_count", "2")) == "POST_TOTAL_MISMATCH"
    assert reason(lambda m: m["post"]["receivables"].__setitem__("leaf_count", "0")) == "POST_TOTAL_MISMATCH"
    assert reason(lambda m: m["liabilities"].__setitem__("converted_nonzero_count", "2")) == "POST_TOTAL_MISMATCH"

    # under MIXED the post figures may be larger, never smaller
    def mixed(fn):
        def apply(m):
            m["post"]["scope"] = "MIXED"
            fn(m)
        return reason(apply)

    assert mixed(lambda m: None) is None
    assert mixed(lambda m: m["post"]["liabilities"].__setitem__("total", "999999")) is None
    assert mixed(lambda m: m["post"]["liabilities"].__setitem__("leaf_count", "9")) is None
    assert mixed(lambda m: m["post"]["liabilities"].__setitem__("total", "322579")) == "POST_TOTAL_MISMATCH"
    assert mixed(lambda m: m["post"]["receivables"].__setitem__("leaf_count", "0")) == "POST_TOTAL_MISMATCH"


def test_residue_bound_follows_the_leaf_count(w2):
    # three leaves: |sum of residues| can be at most 3/2
    manifest = w2.manifest

    def with_converted_total(total: int):
        m = copy.deepcopy(manifest)
        exact = Fraction(10000000, 31)
        m["liabilities"]["conversion"]["total"] = str(total)
        m["liabilities"]["reconciliation_difference"] = str(total - 322581)
        residues = total - exact
        m["liabilities"]["sum_of_residues"] = {"num": str(residues.numerator), "den": str(residues.denominator)}
        m["post"]["liabilities"]["total"] = str(total)
        return verify_conversion_manifest(m).reason

    assert with_converted_total(322580) is None          # the genuine total: -20/31
    assert with_converted_total(322581) is None          # +11/31
    assert with_converted_total(322582) is None          # +42/31, below 3/2
    assert with_converted_total(322579) == "RESIDUE_OUT_OF_BOUND"   # -51/31, beyond 3/2
    assert with_converted_total(322583) == "RESIDUE_OUT_OF_BOUND"   # +73/31
    assert with_converted_total(0) == "RESIDUE_OUT_OF_BOUND"


# ---------------------------------------------------------------------------
# 13. Cheating that keeps the manifest consistent with itself
# ---------------------------------------------------------------------------


def forged(example, *, adjust=None, post=None, scope="CONVERSION_ONLY", others=()) -> ConversionEvidence:
    """Evidence for a conversion that was carried out wrongly, with a self-consistent manifest."""
    declaration = load_declaration(example["manifest"]["declaration"])
    records = []
    post_balances = []
    for holder in example["holders"]:
        x = int(holder["signed_source_amount"])
        y = convert(declaration, x).converted_amount + (adjust or {}).get(holder["source_slot_id"], 0)
        records.append((holder["source_slot_id"], x, holder["target_slot_id"], y))
        if y != 0:
            post_balances.append((holder["target_slot_id"], y))
    return evidence_mod._evidence_from_parts(
        declaration,
        institution_id=example["institution_id"],
        salt_key=bytes.fromhex(example["salt_key_hex"]),
        effective_at=example["effective_at"],
        accounts=example["accounts"],
        records=records if post is None else post[0],
        post_balances=(post_balances if post is None else post[1]) + list(others),
        scope=scope,
        residue_account_change=int(example["residue_account_change_input"]),
    )


def test_forging_helper_reproduces_the_genuine_evidence_when_nothing_is_changed():
    assert forged(EXAMPLES[W2]).manifest_hash == EXAMPLES[W2]["manifest_hash"]


def test_one_holder_of_three_short_changed_is_caught_by_the_manifest_check_alone():
    cheat = forged(EXAMPLES[W2], adjust={"acct-bob": -1})
    m = cheat.manifest
    assert m["liabilities"]["conversion"]["total"] == "322579"
    assert m["liabilities"]["sum_of_residues"] == {"num": "-51", "den": "31"}
    assert verify_conversion_manifest(m).reason == "RESIDUE_OUT_OF_BOUND"
    assert check_bundle(cheat.bundle_for("acct-alice"), m, "acct-alice").reason == "RESIDUE_OUT_OF_BOUND"
    assert audit_conversion_export(cheat.auditor_export(), expected_manifest_hash=cheat.manifest_hash).reason == "RESIDUE_OUT_OF_BOUND"


def test_one_holder_over_credited_is_caught_by_that_holder_and_by_the_auditor():
    cheat = forged(EXAMPLES[W2], adjust={"acct-bob": +1})
    m = cheat.manifest
    assert m["liabilities"]["conversion"]["total"] == "322581"
    assert verify_conversion_manifest(m).ok  # +11/31 is within the bound: the manifest alone cannot tell
    bob = check_bundle(cheat.bundle_for("acct-bob"), m, "acct-bob")
    assert bob.reason == "CONVERTED_AMOUNT_MISMATCH"
    assert (bob.details["expected"], bob.details["recorded"]) == (81494, 81495)
    assert check_bundle(cheat.bundle_for("acct-alice"), m, "acct-alice").ok  # another holder's bundle still verifies
    assert check_bundle(cheat.bundle_for("acct-carol"), m, "acct-carol").ok
    assert audit_conversion_export(cheat.auditor_export(), expected_manifest_hash=cheat.manifest_hash).reason == "CONVERTED_AMOUNT_MISMATCH"


def test_short_change_hidden_behind_another_holders_gain_is_caught_by_both_holders():
    cheat = forged(EXAMPLES[W2], adjust={"acct-bob": -1, "acct-carol": +1})
    m = cheat.manifest
    assert m["liabilities"]["conversion"]["total"] == "322580"  # the totals look exactly right
    assert verify_conversion_manifest(m).ok
    assert check_bundle(cheat.bundle_for("acct-bob"), m, "acct-bob").reason == "CONVERTED_AMOUNT_MISMATCH"
    assert check_bundle(cheat.bundle_for("acct-carol"), m, "acct-carol").reason == "CONVERTED_AMOUNT_MISMATCH"
    assert check_bundle(cheat.bundle_for("acct-alice"), m, "acct-alice").ok
    assert audit_conversion_export(cheat.auditor_export(), expected_manifest_hash=cheat.manifest_hash).reason == "CONVERTED_AMOUNT_MISMATCH"


def _two_sources_one_target(scope, others=()):
    example = EXAMPLES[W2]
    declaration = load_declaration(example["manifest"]["declaration"])
    records, post_balances = [], []
    for holder in example["holders"]:
        x = int(holder["signed_source_amount"])
        y = convert(declaration, x).converted_amount
        target = "acct-alice-w" if holder["source_slot_id"] == "acct-bob" else holder["target_slot_id"]  # bob -> alice's target
        records.append((holder["source_slot_id"], x, target, y))
        if y != 0 and holder["source_slot_id"] != "acct-bob":
            post_balances.append((holder["target_slot_id"], y))  # the shared target is credited once
    return forged(example, post=(records, post_balances), scope=scope, others=others)


@pytest.mark.parametrize("scope", ["CONVERSION_ONLY", "MIXED"])
def test_two_sources_pointed_at_one_target(scope):
    cheat = _two_sources_one_target(scope)
    m = cheat.manifest
    assert m["post"]["scope"] == scope
    assert int(m["post"]["liabilities"]["total"]) < int(m["liabilities"]["conversion"]["total"])
    assert verify_conversion_manifest(m).reason == "POST_TOTAL_MISMATCH"
    assert check_bundle(cheat.bundle_for("acct-bob"), m, "acct-bob").reason == "POST_TOTAL_MISMATCH"


def test_two_sources_one_target_hidden_behind_other_balances_is_caught_by_the_holder_and_the_auditor():
    # Under MIXED, an unrelated balance can make up the missing total, so the manifest alone passes.
    cheat = _two_sources_one_target("MIXED", others=[("acct-other-w", 100_000)])
    m = cheat.manifest
    assert verify_conversion_manifest(m).ok
    bob_bundle = cheat.bundle_for("acct-bob")  # its post proof is for alice's target, which holds alice's amount
    assert check_bundle(bob_bundle, m, "acct-bob").reason == "POST_AMOUNT_MISMATCH"
    assert check_bundle(cheat.bundle_for("acct-alice"), m, "acct-alice").ok
    assert audit_conversion_export(cheat.auditor_export(), expected_manifest_hash=cheat.manifest_hash).reason == "TARGET_NOT_UNIQUE"


def test_known_limit_two_holders_with_equal_amounts_shown_one_target_leaf():
    # A limit the design states (identifiers are chosen by the operator): if two holders with
    # equal balances are pointed at one target leaf, and a made-up leaf fills the gap in the
    # total, neither the manifest check nor either holder's check can notice. Each holder has
    # to know that the target identifier is theirs alone. The full recomputation does notice.
    example = EXAMPLES[W2]
    declaration = load_declaration(example["manifest"]["declaration"])
    y = convert(declaration, 120_000).converted_amount
    records = [("acct-bob", 120_000, "acct-shared-w", y), ("acct-bea", 120_000, "acct-shared-w", y)]
    cheat = evidence_mod._evidence_from_parts(
        declaration,
        institution_id=example["institution_id"],
        salt_key=bytes.fromhex(example["salt_key_hex"]),
        effective_at=example["effective_at"],
        accounts=example["accounts"],
        records=records,
        post_balances=[("acct-shared-w", y), ("acct-other-w", y)],  # the shared target is credited once
        scope="MIXED",
        residue_account_change=0,
    )
    m = cheat.manifest
    assert verify_conversion_manifest(m).ok
    assert check_bundle(cheat.bundle_for("acct-bob"), m, "acct-bob").ok
    assert check_bundle(cheat.bundle_for("acct-bea"), m, "acct-bea").ok
    assert audit_conversion_export(cheat.auditor_export(), expected_manifest_hash=cheat.manifest_hash).reason == "TARGET_NOT_UNIQUE"
    # the declared scope makes no difference: totals and counts agree under CONVERSION_ONLY too
    export = cheat.auditor_export()
    export["manifest"]["post"]["scope"] = "CONVERSION_ONLY"
    assert verify_conversion_manifest(export["manifest"]).ok
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert audit_conversion_export(export, expected_manifest_hash=digest).reason == "TARGET_NOT_UNIQUE"
    # without the made-up leaf the gap shows in the manifest, for anyone
    honest_post = evidence_mod._evidence_from_parts(
        declaration, institution_id=example["institution_id"], salt_key=bytes.fromhex(example["salt_key_hex"]),
        effective_at=example["effective_at"], accounts=example["accounts"], records=records,
        post_balances=[("acct-shared-w", y)], scope="CONVERSION_ONLY", residue_account_change=0,
    )
    assert verify_conversion_manifest(honest_post.manifest).reason == "POST_TOTAL_MISMATCH"


def test_manifest_that_states_other_totals_than_its_roots_bind_is_caught_by_holders_and_the_auditor():
    # Short-change bob by one, then state the honest-looking totals next to the cheating roots.
    cheat = forged(EXAMPLES[W2], adjust={"acct-bob": -1})
    lying = cheat.manifest
    lying["liabilities"]["conversion"]["total"] = "322580"
    lying["liabilities"]["reconciliation_difference"] = "-1"
    lying["liabilities"]["sum_of_residues"] = {"num": "-20", "den": "31"}
    lying["post"]["liabilities"]["total"] = "322580"
    assert verify_conversion_manifest(lying).ok  # the stated figures are consistent with each other
    lying_hash = sha256_hex(canonical_json(lying))

    def bundle_for(slot):
        bundle = cheat.bundle_for(slot)
        bundle["manifest_hash"] = lying_hash
        return bundle

    # every holder notices: the roots bind another total than the manifest states
    assert check_bundle(bundle_for("acct-alice"), lying, "acct-alice").reason == "RECORD_HEAD_MISMATCH"
    assert check_bundle(bundle_for("acct-carol"), lying, "acct-carol").reason == "RECORD_HEAD_MISMATCH"
    assert check_bundle(bundle_for("acct-bob"), lying, "acct-bob").reason == "RECORD_HEAD_MISMATCH"
    export = cheat.auditor_export()
    export["manifest"] = lying
    assert audit_conversion_export(export, expected_manifest_hash=lying_hash).reason == "CONVERSION_HEAD_MISMATCH"


def _restated(manifest: dict, fn) -> tuple[dict, str]:
    """A copy of ``manifest`` changed by ``fn``; it must still pass the manifest check."""
    changed = copy.deepcopy(manifest)
    fn(changed)
    assert verify_conversion_manifest(changed).ok  # the stated figures are consistent with each other
    return changed, sha256_hex(canonical_json(changed))


def _rehashed(bundle: dict, manifest_hash: str) -> dict:
    changed = copy.deepcopy(bundle)
    changed["manifest_hash"] = manifest_hash
    return changed


def test_manifest_that_states_another_pre_or_post_total_than_the_root_binds():
    # pre: one more yen stated next to the genuine root, with every derived figure restated
    # so that the manifest is consistent with itself
    evidence = assemble(EXAMPLES[W2])
    declaration = load_declaration(evidence.manifest["declaration"])

    def more_pre(m):
        side = m["liabilities"]
        exact = declaration.factor * (int(side["pre"]["total"]) + 1)
        rounded = round_fraction(exact, declaration.rounding_mode)
        conversion_total = int(side["conversion"]["total"])
        side["pre"]["total"] = str(int(side["pre"]["total"]) + 1)
        side["exact_total"] = fraction_to_document(exact)
        side["rounded_exact_total"] = str(rounded)
        side["reconciliation_difference"] = str(conversion_total - rounded)
        side["sum_of_residues"] = fraction_to_document(conversion_total - exact)

    lying, lying_hash = _restated(evidence.manifest, more_pre)
    for slot in ("acct-alice", "acct-bob", "acct-carol"):
        assert check_bundle(_rehashed(evidence.bundle_for(slot), lying_hash), lying, slot).reason == "PRE_HEAD_MISMATCH"
    # the debtor's side was not restated: their bundle still verifies against the same manifest
    assert check_bundle(_rehashed(evidence.bundle_for("acct-dave"), lying_hash), lying, "acct-dave").ok
    export = evidence.auditor_export()
    export["manifest"] = lying
    assert audit_conversion_export(export, expected_manifest_hash=lying_hash).reason == "PRE_HEAD_MISMATCH"

    # post: under MIXED a larger post total is consistent with the records, but the root binds another
    mixed = assemble(EXAMPLES[MIXED])
    lying, lying_hash = _restated(
        mixed.manifest,
        lambda m: m["post"]["liabilities"].__setitem__("total", str(int(m["post"]["liabilities"]["total"]) + 1)),
    )
    for slot in ("acct-alice", "acct-bob", "acct-carol"):
        assert check_bundle(_rehashed(mixed.bundle_for(slot), lying_hash), lying, slot).reason == "POST_HEAD_MISMATCH"
    export = mixed.auditor_export()
    export["manifest"] = lying
    assert audit_conversion_export(export, expected_manifest_hash=lying_hash).reason == "POST_HEAD_MISMATCH"


def test_a_debtors_bundle_reports_magnitudes_and_the_side_carries_the_sign():
    example = EXAMPLES[W2]
    declaration = load_declaration(example["manifest"]["declaration"])
    details = check_bundle(assemble(example).bundle_for("acct-dave"), example["manifest"], "acct-dave").details
    signed = convert(declaration, -60)
    assert (signed.converted_amount, signed.exact, signed.residue) == (-41, Fraction(-24000, 589), Fraction(-149, 589))
    assert details["side"] == "receivables"
    assert (details["source_amount"], details["converted_amount"], details["exact"], details["residue"]) == (
        60, 41, Fraction(24000, 589), Fraction(149, 589))


# ---------------------------------------------------------------------------
# 14. A record with amount 0
# ---------------------------------------------------------------------------


def test_record_with_amount_zero_has_no_post_proof():
    example = EXAMPLES[W0]
    evidence = assemble(example)
    m = evidence.manifest
    assert m["liabilities"]["conversion"]["leaf_count"] == "3" and m["liabilities"]["converted_nonzero_count"] == "2"
    assert m["post"]["liabilities"]["leaf_count"] == "2"
    zed = evidence.bundle_for("acct-zed")
    assert zed["post_proof"] is None
    assert zed["record_proof"]["leaf"]["amount"] == "0"
    result = check_bundle(zed, m, "acct-zed")
    assert result.ok and result.details["converted_amount"] == 0
    assert (result.details["source_amount"], result.details["exact"], result.details["residue"]) == (
        73, Fraction(292, 589), Fraction(-292, 589),
    )
    assert result.details["target_slot_id"] == "acct-zed-w"
    with pytest.raises(KeyError):
        evidence.tree("liabilities", "post").proof_for("acct-zed-w")

    with_post = copy.deepcopy(zed)
    with_post["post_proof"] = evidence.bundle_for("acct-yan")["post_proof"]
    assert check_bundle(with_post, m, "acct-zed").reason == "POST_PROOF_UNEXPECTED"
    # and a holder with a converted amount cannot be given a bundle without a post proof
    yan = evidence.bundle_for("acct-yan")
    yan["post_proof"] = None
    assert check_bundle(yan, m, "acct-yan").reason == "POST_PROOF_MISSING"


def test_record_payload_rules():
    example = EXAMPLES[W2]
    declaration = load_declaration(example["manifest"]["declaration"])
    alice_y = convert(declaration, 300_000).converted_amount

    def with_payload(payload: str):
        evidence = forged(example)
        # rebuild the liabilities records tree with one payload replaced
        tree = evidence.tree("liabilities", "conversion")
        items = [(l.slot_id, l.amount, payload if l.slot_id == "acct-alice" else l.payload) for l in tree.leaves]
        from openreserve.transparency.sumtree import SumTree

        replaced = SumTree.build(tree.context, items, salt_key=bytes.fromhex(example["salt_key_hex"]))
        manifest = evidence.manifest
        manifest["liabilities"]["conversion"] = replaced.head()
        bundle = evidence.bundle_for("acct-alice")
        bundle["record_proof"] = replaced.proof_for("acct-alice")
        bundle["manifest_hash"] = sha256_hex(canonical_json(manifest))
        export = evidence.auditor_export()
        export["manifest"] = manifest
        export["liabilities"]["conversion"] = replaced.export()
        return (
            check_bundle(bundle, manifest, "acct-alice").reason,
            audit_conversion_export(export, expected_manifest_hash=bundle["manifest_hash"]).reason,
        )

    good = '{"source_amount":"300000","target_slot_id":"acct-alice-w"}'
    assert with_payload(good) == (None, None)
    for payload in (
        "",
        "{}",
        "not json",
        '{"source_amount":"300000"}',
        '{"source_amount":"300000","target_slot_id":"acct-alice-w","note":"x"}',
        '{"source_amount":"300000","target_slot_id":"acct-alice-w","zzz":"x"}',  # an extra key, in canonical order
        '{"extra":"x","source_amount":"300000","target_slot_id":"acct-alice-w"}',
        '{"source_amount":300000,"target_slot_id":"acct-alice-w"}',           # a JSON number
        '{"source_amount":"0300000","target_slot_id":"acct-alice-w"}',
        '{"source_amount":"-300000","target_slot_id":"acct-alice-w"}',
        '{"source_amount":"300000","target_slot_id":""}',
        '{"target_slot_id":"acct-alice-w","source_amount":"300000"}',         # keys not in canonical order
        '{"source_amount": "300000", "target_slot_id": "acct-alice-w"}',      # whitespace
        '{"source_amount":"300000","source_amount":"300000","target_slot_id":"acct-alice-w"}',  # a repeated key
        '[{"source_amount":"300000","target_slot_id":"acct-alice-w"}]',
        "[" * 3000,
    ):
        assert with_payload(payload) == ("RECORD_PAYLOAD_MALFORMED", "RECORD_PAYLOAD_MALFORMED"), payload
    assert with_payload('{"source_amount":"299999","target_slot_id":"acct-alice-w"}') == (
        "SOURCE_AMOUNT_MISMATCH", "SOURCE_AMOUNT_MISMATCH",
    )
    # another target in the record: the holder is sent to a leaf that is not theirs
    holder_reason, auditor_reason = with_payload('{"source_amount":"300000","target_slot_id":"acct-bob-w"}')
    assert holder_reason == "POST_SLOT_MISMATCH" and auditor_reason == "TARGET_NOT_UNIQUE"
    assert alice_y == 203_735


# ---------------------------------------------------------------------------
# Auditor export: exact reasons
# ---------------------------------------------------------------------------


def test_auditor_export_reasons(w2):
    export, manifest_hash = w2.auditor_export(), w2.manifest_hash

    def reason(fn, h=manifest_hash):
        return audit_conversion_export(_changed(export, fn), expected_manifest_hash=h).reason

    assert reason(lambda e: None) is None
    assert reason(lambda e: None, h=OTHER_HASH) == "MANIFEST_HASH_MISMATCH"
    first = lambda e, side, stage: e[side][stage]["leaves"][0]
    plus_one = lambda leaf: leaf.__setitem__("amount", str(int(leaf["amount"]) + 1))
    assert reason(lambda e: plus_one(first(e, "liabilities", "pre"))) == "PRE_ROOT_MISMATCH"
    assert reason(lambda e: plus_one(first(e, "receivables", "pre"))) == "PRE_ROOT_MISMATCH"
    assert reason(lambda e: plus_one(first(e, "liabilities", "conversion"))) == "CONVERSION_ROOT_MISMATCH"
    assert reason(lambda e: plus_one(first(e, "liabilities", "post"))) == "POST_ROOT_MISMATCH"
    assert reason(lambda e: e["liabilities"]["post"]["leaves"].reverse()) == "POST_NOT_IN_CANONICAL_ORDER"
    assert reason(lambda e: e["liabilities"]["pre"]["leaves"].pop()) == "PRE_ROOT_MISMATCH"
    assert reason(lambda e: first(e, "liabilities", "pre").__setitem__("amount", "-1")) == "PRE_MALFORMED_EXPORT"
    assert reason(lambda e: e["liabilities"]["conversion"].__setitem__("format", "x")) == "CONVERSION_UNSUPPORTED_FORMAT"
    assert reason(lambda e: e["receivables"].__setitem__("post", None)) == "POST_UNSUPPORTED_FORMAT"
    assert reason(lambda e: e["liabilities"]["pre"]["context"].__setitem__("as_of", "2099-12-31T15:00:00.000001Z")) == "PRE_CONTEXT_MISMATCH"
    assert reason(lambda e: e["liabilities"]["conversion"]["context"].__setitem__("binding", "")) == "CONVERSION_CONTEXT_MISMATCH"
    assert reason(lambda e: e["liabilities"]["post"]["context"].__setitem__("kind", "receivables")) == "POST_CONTEXT_MISMATCH"
    assert reason(lambda e: e["liabilities"].__setitem__("post", e["receivables"]["post"])) == "POST_CONTEXT_MISMATCH"
    assert reason(lambda e: e["manifest"]["liabilities"]["conversion"].__setitem__("total", "322581")) == "RESIDUE_MISMATCH"
    assert reason(lambda e: e["manifest"].pop("post")) == "MALFORMED_MANIFEST"
    for fn in (
        lambda e: e.pop("receivables"),
        lambda e: e.__setitem__("extra", "x"),
        lambda e: e.__setitem__("format", "openreserve.conversion.export/2"),
        lambda e: e["liabilities"].pop("post"),
        lambda e: e["liabilities"].__setitem__("extra", {}),
        lambda e: e.__setitem__("liabilities", None),
        lambda e: first(e, "liabilities", "pre").__setitem__("amount", 300000),  # a JSON number
    ):
        assert reason(fn) == "MALFORMED_EXPORT"
    for document in (None, [], "export", {}, w2.manifest):
        assert audit_conversion_export(document, expected_manifest_hash=manifest_hash).reason == "MALFORMED_EXPORT"
    with pytest.raises(ValueError):
        audit_conversion_export(export, expected_manifest_hash="xyz")


def test_auditor_finds_structural_cheating():
    example = EXAMPLES[W2]
    declaration = load_declaration(example["manifest"]["declaration"])

    def audit(evidence):
        return audit_conversion_export(evidence.auditor_export(), expected_manifest_hash=evidence.manifest_hash).reason

    honest_records = []
    honest_post = []
    for holder in example["holders"]:
        x = int(holder["signed_source_amount"])
        y = convert(declaration, x).converted_amount
        honest_records.append((holder["source_slot_id"], x, holder["target_slot_id"], y))
        if y != 0:
            honest_post.append((holder["target_slot_id"], y))
    assert audit(forged(example, post=(honest_records, honest_post))) is None

    # an extra post leaf that is no target of the conversion, declared as CONVERSION_ONLY:
    # a zero-amount leaf keeps the post total equal, so only the leaf count gives it away
    extra = forged(example, post=(honest_records, honest_post + [("acct-ghost-w", 7)]))
    assert verify_conversion_manifest(extra.manifest).reason == "POST_TOTAL_MISMATCH"
    # a target credited with another amount than its record, while another target makes up the total
    shifted_post = [(t, y + 1 if t == "acct-alice-w" else (y - 1 if t == "acct-bob-w" else y)) for t, y in honest_post]
    shifted = forged(example, post=(honest_records, shifted_post))
    assert verify_conversion_manifest(shifted.manifest).ok
    assert audit(shifted) == "POST_AMOUNT_MISMATCH"
    assert check_bundle(shifted.bundle_for("acct-carol"), shifted.manifest, "acct-carol").ok
    # a post leaf under another slot than the record's target
    renamed_post = [("acct-mallory-w" if t == "acct-bob-w" else t, y) for t, y in honest_post]
    renamed = forged(example, post=(honest_records, renamed_post))
    assert verify_conversion_manifest(renamed.manifest).ok
    assert audit(renamed) == "POST_AMOUNT_MISMATCH"
    # under MIXED an unrelated leaf is allowed, under CONVERSION_ONLY it is not
    mixed = forged(example, post=(honest_records, honest_post), scope="MIXED", others=[("acct-other-w", 500)])
    assert audit(mixed) is None
    assert mixed.manifest_hash == EXAMPLES[MIXED]["manifest_hash"]


def test_auditor_finds_records_that_do_not_match_the_pre_snapshot(w2):
    example = EXAMPLES[W2]
    key = bytes.fromhex(example["salt_key_hex"])
    from openreserve.transparency.sumtree import SumTree

    evidence = forged(example)
    tree = evidence.tree("liabilities", "conversion")
    # replace carol's record by a record for a slot that is not in the pre snapshot
    items = [("acct-zzz" if l.slot_id == "acct-carol" else l.slot_id, l.amount, l.payload) for l in tree.leaves]
    replaced = SumTree.build(tree.context, items, salt_key=key)
    export = evidence.auditor_export()
    export["manifest"]["liabilities"]["conversion"] = replaced.head()
    export["liabilities"]["conversion"] = replaced.export()
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert audit_conversion_export(export, expected_manifest_hash=digest).reason == "RECORDS_DO_NOT_MATCH_PRE_SNAPSHOT"

    # a slot that is both a liability and a receivable before the conversion
    pre_receivables = evidence.tree("receivables", "pre")
    doubled = SumTree.build(pre_receivables.context, [("acct-alice", 60)], salt_key=key)
    export = evidence.auditor_export()
    export["manifest"]["receivables"]["pre"] = doubled.head()
    export["receivables"]["pre"] = doubled.export()
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert audit_conversion_export(export, expected_manifest_hash=digest).reason == "PRE_SLOT_IN_BOTH_TREES"

    # a target that is both a liability and a receivable after the conversion (same total and
    # count as the genuine post receivables tree, so the manifest stays consistent)
    post_receivables = evidence.tree("receivables", "post")
    assert [(l.slot_id, l.amount) for l in post_receivables.leaves] == [("acct-dave-w", 41)]
    doubled = SumTree.build(post_receivables.context, [("acct-alice-w", 41)], salt_key=key)
    export = evidence.auditor_export()
    export["manifest"]["post"]["receivables"] = doubled.head()
    export["receivables"]["post"] = doubled.export()
    assert verify_conversion_manifest(export["manifest"]).ok
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert audit_conversion_export(export, expected_manifest_hash=digest).reason == "POST_SLOT_IN_BOTH_TREES"


def test_auditor_finds_a_post_leaf_that_is_no_target_and_a_wrong_nonzero_count():
    from openreserve.transparency.sumtree import SumTree

    example = EXAMPLES[W0]  # three records, one of them converts to 0
    key = bytes.fromhex(example["salt_key_hex"])
    evidence = assemble(example)
    post = evidence.tree("liabilities", "post")
    assert post.leaf_count == 2

    # A ghost leaf with amount 0 in the post tree, and a manifest that counts it as converted:
    # every stated figure is consistent, so only the full recomputation finds it.
    ghost = SumTree.build(post.context, [(l.slot_id, l.amount) for l in post.leaves] + [("acct-ghost-w", 0)], salt_key=key)
    export = evidence.auditor_export()
    export["liabilities"]["post"] = ghost.export()
    export["manifest"]["post"]["liabilities"] = ghost.head()
    export["manifest"]["liabilities"]["converted_nonzero_count"] = "3"
    assert verify_conversion_manifest(export["manifest"]).ok
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert audit_conversion_export(export, expected_manifest_hash=digest).reason == "POST_HAS_UNCONVERTED_LEAVES"

    # Under MIXED such a leaf is allowed, but the stated count of non-zero records must still be true.
    export["manifest"]["post"]["scope"] = "MIXED"
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert verify_conversion_manifest(export["manifest"]).ok
    assert audit_conversion_export(export, expected_manifest_hash=digest).reason == "COUNT_MISMATCH"
    export["manifest"]["liabilities"]["converted_nonzero_count"] = "2"
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert audit_conversion_export(export, expected_manifest_hash=digest).ok
    export["manifest"]["liabilities"]["converted_nonzero_count"] = "1"
    digest = sha256_hex(canonical_json(export["manifest"]))
    assert verify_conversion_manifest(export["manifest"]).ok
    assert audit_conversion_export(export, expected_manifest_hash=digest).reason == "COUNT_MISMATCH"


# ---------------------------------------------------------------------------
# 15. Seeded mutations
# ---------------------------------------------------------------------------


def test_seeded_mutations_of_a_manifest_never_raise_and_never_pass_under_the_published_hash(w2):
    rng = random.Random(15_1)
    manifest, manifest_hash = w2.manifest, w2.manifest_hash
    bundles = {slot: w2.bundle_for(slot) for slot in w2.source_slot_ids()}
    done = accepted_alone = 0
    while done < 4_500:
        candidate = mutate(manifest, rng)
        if _same(candidate, manifest):
            continue
        done += 1
        alone = verify_conversion_manifest(candidate)
        inner, _ = evidence_mod._check_manifest(candidate)
        assert (alone.ok, alone.reason) == (inner.ok, inner.reason)
        if alone.ok:
            # a changed manifest can be consistent with itself, but then it has another hash
            accepted_alone += 1
            assert alone.details["manifest_hash"] != manifest_hash
        slot = rng.choice(list(bundles))
        result = verify_conversion_bundle(
            bundles[slot], candidate, expected_manifest_hash=manifest_hash, expected_source_slot_id=slot, expected_side=side_of(slot)
        )
        assert result.ok is False
    assert accepted_alone > 0  # the case above does occur (for example a changed root)


def test_seeded_mutations_of_a_bundle_never_raise_and_are_never_accepted():
    rng = random.Random(15_2)
    evidences = [assemble(EXAMPLES[name]) for name in (W2, W0, MIXED)]
    done = accepted = 0
    reasons: dict[str, int] = {}
    while done < 4_500:
        evidence = rng.choice(evidences)
        slot = rng.choice(evidence.source_slot_ids())
        genuine = evidence.bundle_for(slot)
        candidate = mutate(genuine, rng)
        if _same(candidate, genuine):
            continue
        done += 1
        manifest = evidence.manifest
        inner = evidence_mod._verify_bundle(candidate, manifest, evidence.manifest_hash, slot, side_of(slot))
        result = verify_conversion_bundle(
            candidate, manifest, expected_manifest_hash=evidence.manifest_hash, expected_source_slot_id=slot,
            expected_side=side_of(slot),
        )
        assert (inner.ok, inner.reason) == (result.ok, result.reason)
        accepted += result.ok
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
    assert accepted == 0
    assert {"MALFORMED_BUNDLE", "PRE_ROOT_MISMATCH", "RECORD_ROOT_MISMATCH", "POST_ROOT_MISMATCH"} <= set(reasons)


def test_seeded_mutations_of_an_export_never_raise_and_are_never_accepted():
    rng = random.Random(15_3)
    evidences = [assemble(EXAMPLES[name]) for name in (W2, W0, MIXED)]
    done = accepted = 0
    reasons: dict[str, int] = {}
    while done < 1_500:
        evidence = rng.choice(evidences)
        genuine = evidence.auditor_export()
        candidate = mutate(genuine, rng)
        if _same(candidate, genuine):
            continue
        done += 1
        inner = evidence_mod._audit_export(candidate, evidence.manifest_hash)
        result = audit_conversion_export(candidate, expected_manifest_hash=evidence.manifest_hash)
        assert (inner.ok, inner.reason) == (result.ok, result.reason)
        accepted += result.ok
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
    assert accepted == 0
    assert {"MALFORMED_EXPORT", "MANIFEST_HASH_MISMATCH"} <= set(reasons)
