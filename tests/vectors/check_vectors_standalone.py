#!/usr/bin/env python3
"""Standalone re-computation of the sum tree and conversion test vectors.

Standard library only, and it imports nothing from the package under test: every value in
sumtree_v1.json, conversion_v1.json and coverage_v1.json is recomputed here from the written
specification and compared.  Usage:  python3 check_vectors_standalone.py [directory]
"""
import hashlib
import hmac
import json
import os
import sys
from fractions import Fraction

DOMAIN = b"openreserve.sumtree.v1"
MAX = 10**38 - 1


def sha(b):
    return hashlib.sha256(b).digest()


def lp(b):
    return len(b).to_bytes(4, "big") + b


def u128(n):
    assert isinstance(n, int) and 0 <= n <= MAX
    return n.to_bytes(16, "big")


def context_digest(c):
    return sha(DOMAIN + b"\x03" + lp(c["kind"].encode()) + lp(c["institution_id"].encode()) + lp(c["currency"].encode())
               + bytes([int(c["minor_units"])]) + lp(c["as_of"].encode()) + lp(bytes.fromhex(c["binding"])))


def salt_for(key, ctx, slot_id):
    return hmac.new(key, DOMAIN + b"\x10" + ctx + lp(slot_id.encode()), hashlib.sha256).digest()


def leaf_hash(ctx, slot_id, amount, salt, payload=""):
    return sha(DOMAIN + b"\x00" + ctx + lp(slot_id.encode()) + u128(amount) + salt + lp(payload.encode()))


def node(left, right):
    (lh, ls), (rh, rs) = left, right
    assert ls + rs <= MAX
    return sha(DOMAIN + b"\x01" + lh + u128(ls) + rh + u128(rs)), ls + rs


def top(nodes):
    if len(nodes) == 1:
        return nodes[0]
    k = 1
    while 2 * k < len(nodes):
        k *= 2
    return node(top(nodes[:k]), top(nodes[k:]))


def root(ctx, count, total, top_hash):
    return sha(DOMAIN + b"\x02" + ctx + count.to_bytes(8, "big") + u128(total) + top_hash).hex()


def fold(leaf, amount, index, count, path):
    """Inclusion check in the style of RFC 9162 section 2.1.3.2, carrying sums."""
    if index >= count:
        return None
    fn, sn, cur = index, count - 1, (leaf, amount)
    for sib in path:
        if sn == 0:
            return None
        if fn & 1 or fn == sn:
            cur = node(sib, cur)
            while not (fn & 1 or fn == 0):
                fn >>= 1
                sn >>= 1
        else:
            cur = node(cur, sib)
        fn >>= 1
        sn >>= 1
    return cur if sn == 0 else None


def check_sumtree(v):
    key = bytes.fromhex(v["salt_key_hex"])
    n_trees = n_proofs = 0
    for t in v["trees"]:
        ctx = context_digest(t["context"])
        assert ctx.hex() == t["context_digest"], t["name"]
        rows = []
        for lf in t["input_leaves"]:
            s = salt_for(key, ctx, lf["slot_id"])
            rows.append((leaf_hash(ctx, lf["slot_id"], int(lf["amount"]), s), lf["slot_id"], int(lf["amount"]), s))
        rows.sort()
        assert [(r[1], str(r[2]), r[3].hex(), r[0].hex()) for r in rows] == \
               [(c["slot_id"], c["amount"], c["salt"], c["leaf_hash"]) for c in t["canonical_leaves"]], t["name"]
        nodes = [(r[0], r[2]) for r in rows]
        th, total = top(nodes) if nodes else (bytes(32), 0)
        assert th.hex() == t["top_hash"] and str(total) == t["head"]["total"] and str(len(rows)) == t["head"]["leaf_count"], t["name"]
        assert root(ctx, len(rows), total, th) == t["head"]["root"], t["name"]
        n_trees += 1
        for item in t.get("proofs", []):
            p = item["proof"]
            i, n = int(p["leaf_index"]), int(p["leaf_count"])
            lh = leaf_hash(context_digest(p["context"]), p["leaf"]["slot_id"], int(p["leaf"]["amount"]), bytes.fromhex(p["leaf"]["salt"]), p["leaf"]["payload"])
            got = fold(lh, int(p["leaf"]["amount"]), i, n, [(bytes.fromhex(s["hash"]), int(s["sum"])) for s in p["path"]])
            assert got is not None and root(context_digest(p["context"]), n, got[1], got[0]) == t["head"]["root"], (t["name"], i)
            assert sum(int(d["sum"]) for d in item["disclosed_sibling_subtrees"]) == total - int(p["leaf"]["amount"])
            assert sum(int(d["leaf_count"]) for d in item["disclosed_sibling_subtrees"]) == n - 1
            n_proofs += 1
    ex = v["byte_layout_example"]
    assert sha(bytes.fromhex(ex["context_preimage_hex"])).hex() == ex["context_digest"]
    assert hmac.new(key, bytes.fromhex(ex["salt_hmac_message_hex"]), hashlib.sha256).hexdigest() == ex["salt"]
    assert sha(bytes.fromhex(ex["leaf_preimage_hex"])).hex() == ex["leaf_hash"]
    assert sha(bytes.fromhex(ex["node_preimage_hex"])).hex() == ex["top_hash"] and len(bytes.fromhex(ex["node_preimage_hex"])) == 119
    assert sha(bytes.fromhex(ex["root_preimage_hex"])).hex() == ex["root"]
    roots = {t["name"]: t["head"]["root"] for t in v["trees"]}
    assert len(set(roots.values())) == len(roots)
    assert roots["abc"] not in [x["root"] for x in v["abc_context_variants"].values()] + [v["abc_root_with_another_salt_key"]["root"]]
    return n_trees, n_proofs


def round_fraction(z, mode):
    sign, mag = (-1 if z < 0 else 1), abs(z)
    q, r = divmod(mag.numerator, mag.denominator)
    twice = 2 * r
    if twice > mag.denominator or (twice == mag.denominator and (mode == "HALF_AWAY_FROM_ZERO" or q % 2 == 1)):
        q += 1
    return sign * q


def rate_fraction(text):
    whole, _, frac = text.partition(".")
    return Fraction(int(whole + frac), 10 ** len(frac))


def factor_of(d):
    scale = Fraction(10) ** (int(d["target"]["minor_units"]) - int(d["source"]["minor_units"]))
    r = rate_fraction(d["rate"]["text"])
    return scale / r if d["rate"]["quotation"] == "SOURCE_PER_TARGET" else scale * r


def fr(o):
    return Fraction(int(o["num"]), int(o["den"]))


def check_conversion(v):
    n = 0
    by_name = {}
    for c in v["cases"]:
        d = c["declaration"]
        by_name[c["name"]] = d
        canon = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        assert canon.hex() == c["declaration_canonical_utf8_hex"] and hashlib.sha256(canon).hexdigest() == c["declaration_hash"], c["name"]
        k = factor_of(d)
        assert k == fr(c["factor"]) and str(k.numerator) == c["factor"]["num"], c["name"]
        for row in c["conversions"]:
            x = int(row["x"])
            z = x * k
            y = round_fraction(z, d["rounding"]["mode"])
            assert z == fr(row["z"]) and y == int(row["y"]) and y - z == fr(row["e"]), (c["name"], x)
            assert abs(y - z) <= Fraction(1, 2) and round_fraction(-z, d["rounding"]["mode"]) == -y
            n += 1
    for b in v["batch_totals"]:
        d = by_name[b["declaration_case"]]
        k, mode = factor_of(d), d["rounding"]["mode"]
        xs = [int(m) for m in b["magnitudes"]]
        ys = [round_fraction(x * k, mode) for x in xs]
        t = b["totals"]
        exact = sum(xs) * k
        assert (str(sum(xs)), str(sum(ys))) == (t["source_total"], t["converted_total"])
        assert exact == fr(t["exact_total"]) and str(round_fraction(exact, mode)) == t["rounded_exact_total"]
        assert str(sum(ys) - round_fraction(exact, mode)) == t["reconciliation_difference"] and sum(ys) - exact == fr(t["sum_of_residues"])
    for r in v["rate_text_valid"]:
        assert rate_fraction(r["text"]) == Fraction(int(r["num"]), int(r["den"]))
        assert len(r["text"].replace(".", "").lstrip("0")) == int(r["significant_figures"])
    i = v["inverse_rate_illustration"]
    assert int(i["y_via_rounded_inverse"]) - int(i["y_exact_division"]) == int(i["difference_minor_units"]) == 11880
    return n


def check_coverage(v):
    for c in v["cases"]:
        res, liab = int(c["reserves"]), int(c["liabilities"])
        assert c["covered"] is (res >= liab)
        assert c["basis_points_floor"] == (None if liab == 0 else str((10000 * res) // liab))
    return len(v["cases"])


if __name__ == "__main__":
    here = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    load = lambda name: json.load(open(os.path.join(here, name), encoding="utf-8"))
    print("sum tree: %d trees, %d inclusion proofs recomputed" % check_sumtree(load("sumtree_v1.json")))
    print("conversion: %d conversions recomputed" % check_conversion(load("conversion_v1.json")))
    print("coverage: %d cases recomputed" % check_coverage(load("coverage_v1.json")))
    print("OK")
