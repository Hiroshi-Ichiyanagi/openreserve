"""
transparency/merkle.py — Merkle Tree実装。

Proof of Reservesの基礎構造。利用者個別の残高を秘匿しつつ、
総額の正しさを暗号学的に検証可能にする。

リーフは (account_id, balance_cents) のハッシュ。
内部ノードは子ノードのハッシュの連結のSHA-256。
ルートが公開され、利用者は自分の残高についてMerkle Pathを使って
「自分の残高がツリーに含まれている」ことを検証できる。

This is the legacy hash-only tree. Its bytes are frozen: a leaf is
SHA-256("LEAF:<id>:<balance>:<currency>"), a node is SHA-256("NODE:" + left + right), and an
odd last node is paired with itself. It keeps verifying artifacts published by 0.1.x and it
keeps serving the intent and execution commitments. Known limit, kept visible by a test: the
total is not bound to the root, and because of the self-pairing two leaf lists with different
totals can share one root. New reserve artifacts use the Merkle-sum tree in
``transparency/sumtree.py`` instead.

There is exactly one verification routine for this tree (``_verify_legacy``).
``MerkleProof.verify()`` and ``verify_proof_with_node_prefix()`` both call it.
"""

from __future__ import annotations

import hashlib
import re
import warnings
from dataclasses import dataclass
from typing import Sequence

_HEX64 = re.compile(r"[0-9a-f]{64}")
# A tree with more than 2**64 leaves cannot exist; a longer path is malformed.
_MAX_STEPS = 64


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_hash(value: object) -> bool:
    """Exactly 64 lower-case hexadecimal characters."""
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _node_hash(left_hex: str, right_hex: str) -> str:
    """The node hash of the legacy tree: SHA-256 over ``NODE:`` and both child hashes."""
    return sha256_hex(b"NODE:" + bytes.fromhex(left_hex) + bytes.fromhex(right_hex))


@dataclass(frozen=True)
class MerkleLeaf:
    """ツリーのリーフ。account_idとbalance_centsを含み、ハッシュが計算される。

    リーフのハッシュ計算には domain separator（"LEAF:"）を含める。
    内部ノードと衝突する second-preimage 攻撃を防ぐ標準手法。
    """

    account_id: str
    balance_cents: int
    currency_code: str

    @property
    def hash_hex(self) -> str:
        payload = f"LEAF:{self.account_id}:{self.balance_cents}:{self.currency_code}".encode()
        return sha256_hex(payload)


@dataclass(frozen=True)
class MerkleProofStep:
    """Merkle Path上の1ステップ。

    sibling_hash: 兄弟ノードのハッシュ
    is_left: 兄弟が左側にあれば True、右側なら False
    """

    sibling_hash: str
    is_left: bool


@dataclass(frozen=True)
class MerkleProof:
    """利用者が自身の残高の包含を証明するためのデータ構造。"""

    leaf: MerkleLeaf
    steps: tuple[MerkleProofStep, ...]
    root_hash: str

    def verify(self, expected_root: str | None = None) -> bool:
        """このProofが有効かどうかを検証する。

        With ``expected_root`` (the root the verifier trusts, for example the published
        one), the root recomputed from the leaf and the path must equal it.

        Without the argument the recomputed root is compared with ``self.root_hash``, the
        root carried inside the proof. That shows only that the proof is consistent with
        itself: whoever made the proof also chose that root. Pass the published root.

        Malformed input gives ``False``, never an exception.
        """
        return _verify_legacy(self, expected_root)


class MerkleTree:
    """残高集合のMerkle Tree。

    リーフ数が奇数の場合は最後のノードを複製してペアを作る（標準的な手法）。
    内部ノードのハッシュ計算には domain separator（"NODE:"）を含める。
    """

    def __init__(self, leaves: Sequence[MerkleLeaf]) -> None:
        if not leaves:
            raise ValueError("Merkle tree requires at least one leaf")
        self._leaves: tuple[MerkleLeaf, ...] = tuple(leaves)
        # 各レベルのノードハッシュを計算し保持する
        self._levels: list[list[str]] = self._build_levels()

    def _build_levels(self) -> list[list[str]]:
        """全レベルのノードハッシュを計算する。レベル0がリーフ、最終レベルがルート。"""
        # レベル0：リーフのハッシュ
        current_level = [leaf.hash_hex for leaf in self._leaves]
        levels = [current_level]

        while len(current_level) > 1:
            next_level: list[str] = []
            i = 0
            while i < len(current_level):
                left = current_level[i]
                right = current_level[i + 1] if i + 1 < len(current_level) else current_level[i]
                # 内部ノードの domain separator
                next_level.append(_node_hash(left, right))
                i += 2
            levels.append(next_level)
            current_level = next_level

        return levels

    @property
    def root_hash(self) -> str:
        return self._levels[-1][0]

    @property
    def leaf_count(self) -> int:
        return len(self._leaves)

    def total_balance_cents(self) -> int:
        """全リーフのbalance_centsの合計（同一通貨想定）。"""
        return sum(leaf.balance_cents for leaf in self._leaves)

    def proof_for(self, account_id: str) -> MerkleProof:
        """指定アカウントについてMerkle Proofを生成する。

        The proof is checked with ``MerkleProof.verify(expected_root=...)``.
        """
        # account_idを持つリーフを探す
        leaf_index = None
        for i, leaf in enumerate(self._leaves):
            if leaf.account_id == account_id:
                leaf_index = i
                break
        if leaf_index is None:
            raise ValueError(f"Account {account_id} not in Merkle tree")

        # Merkle Pathを構築
        steps: list[MerkleProofStep] = []
        current_index = leaf_index
        for level_idx in range(len(self._levels) - 1):
            level = self._levels[level_idx]
            if current_index % 2 == 0:
                # 自分が左、兄弟が右
                sibling_index = current_index + 1
                if sibling_index >= len(level):
                    sibling_index = current_index  # 複製
                steps.append(MerkleProofStep(sibling_hash=level[sibling_index], is_left=False))
            else:
                # 自分が右、兄弟が左
                sibling_index = current_index - 1
                steps.append(MerkleProofStep(sibling_hash=level[sibling_index], is_left=True))
            current_index = current_index // 2

        return MerkleProof(
            leaf=self._leaves[leaf_index],
            steps=tuple(steps),
            root_hash=self.root_hash,
        )


def _verify_legacy(proof: object, expected_root: object = None) -> bool:
    """The one verification routine of the legacy tree.

    Recomputes the root from the leaf and the path with the tree's own node hash and compares
    it with ``expected_root``, or with the proof's own ``root_hash`` when none is given.
    Returns ``False`` for anything malformed: a sibling hash or root that is not exactly 64
    lower-case hexadecimal characters, an ``is_left`` that is not a ``bool``, more than 64
    steps, or an object that is not a proof at all.
    """
    if not isinstance(proof, MerkleProof):
        return False
    target = proof.root_hash if expected_root is None else expected_root
    if not _is_hash(target):
        return False
    steps = proof.steps
    if not isinstance(steps, (tuple, list)) or len(steps) > _MAX_STEPS:
        return False
    if not isinstance(proof.leaf, MerkleLeaf):
        return False
    try:
        current = proof.leaf.hash_hex
    except (UnicodeError, ValueError, TypeError):
        return False
    for step in steps:
        if not isinstance(step, MerkleProofStep):
            return False
        if type(step.is_left) is not bool or not _is_hash(step.sibling_hash):
            return False
        if step.is_left:
            current = _node_hash(step.sibling_hash, current)
        else:
            current = _node_hash(current, step.sibling_hash)
    return current == target


def verify_proof_with_node_prefix(proof: MerkleProof) -> bool:
    """Another name for ``proof.verify()``: the one legacy verifier, without an expected root.

    Kept because published guides and examples call it. It compares with the root carried in
    the proof, so the caller must still compare ``proof.root_hash`` with the published root;
    ``proof.verify(expected_root=published_root)`` does both. Calling it emits a
    ``PendingDeprecationWarning`` that names that replacement.
    """
    warnings.warn(
        "verify_proof_with_node_prefix(proof) will be deprecated; use "
        "proof.verify(expected_root=published_root), which also compares the published root",
        PendingDeprecationWarning,
        stacklevel=2,
    )
    return _verify_legacy(proof, None)
