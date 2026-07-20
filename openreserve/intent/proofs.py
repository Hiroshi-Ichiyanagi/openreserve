"""
intent/proofs.py — 委任枠（Mandate）に対する決定論的な証明。

インテントベースの自律決済で最も重要な信頼命題は
**「自律エージェントが一度も枠を超えていない」** ことを、運営者を信頼せずに
第三者が後から検証できることである。ProofOfMandate はそれを 1 つの公開可能な
アーティファクトにまとめる：

- 予算不変条件：remaining = budget_cap - 累計消化 >= 0（枠を超えていない）
- 担保条件：escrow 残高 >= remaining（残枠が資金で裏付けられている）
- 受取人 Merkle ルート：各 payee は自分の受領累計が含まれることを、他者の
  受領額を知らずに Merkle 包含証明で検証できる（proof_of_reserves と同じ手法）
- 監査ログのコミットメント：成立・却下の全判断を刻んだハッシュチェーンの先頭ハッシュ

ProofOfReservesGenerator と同じ決定論規律に従う：snapshot_at は必須で、
ウォールクロック（datetime.now()）にはフォールバックしない。同一の元帳状態 +
同一の snapshot_at は、同一の Merkle ルート・同一の集計値を再現する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from openreserve.intent.mandate import IntentSettlementEngine
from openreserve.transparency.audit_log import GENESIS_HASH
from openreserve.transparency.merkle import MerkleLeaf, MerkleTree

# payee が 0 件のときの Merkle ツリー用プレースホルダ（ProofOfReserves と同じ流儀）
_EMPTY_LEAF_ID = "__empty__"


@dataclass(frozen=True)
class ProofOfMandate:
    """ある時点における委任枠の消化・担保・履歴の証明。"""

    snapshot_at: datetime
    mandate_id: str
    principal_id: str
    currency_code: str
    budget_cap_cents: int
    total_settled_cents: int
    remaining_cents: int
    settlement_count: int
    payee_count: int
    escrow_account_id: str
    escrow_balance_cents: int
    is_within_budget: bool  # remaining_cents >= 0
    is_collateralized: bool  # escrow_balance_cents >= remaining_cents
    payee_receipts_root: str
    valid_from: datetime
    valid_until: datetime
    audit_log_root_hash: str

    def to_public_summary(self) -> dict[str, Any]:
        """公開ダッシュボード用の要約。個別 payee の受領額は含まない（Merkle ルートのみ）。"""
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "mandate_id": self.mandate_id,
            "principal_id": self.principal_id,
            "currency": self.currency_code,
            "budget_cap_cents": self.budget_cap_cents,
            "total_settled_cents": self.total_settled_cents,
            "remaining_cents": self.remaining_cents,
            "settlement_count": self.settlement_count,
            "payee_count": self.payee_count,
            "escrow_account_id": self.escrow_account_id,
            "escrow_balance_cents": self.escrow_balance_cents,
            "is_within_budget": self.is_within_budget,
            "is_collateralized": self.is_collateralized,
            "payee_receipts_merkle_root": self.payee_receipts_root,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "audit_log_committed": self.audit_log_root_hash,
        }


class ProofOfMandateGenerator:
    """IntentSettlementEngine から ProofOfMandate を生成する。

    集計値はすべてエンジン経由で元帳から再構成される（別カウンタを信頼しない）。
    """

    def __init__(self, engine: IntentSettlementEngine) -> None:
        self._engine = engine

    def generate(
        self,
        mandate_id: str,
        snapshot_at: datetime,
    ) -> tuple[ProofOfMandate, MerkleTree]:
        """snapshot_at 時点の委任枠証明を生成する。

        snapshot_at は必須（ウォールクロック・フォールバックなし）。
        Returns: (ProofOfMandate, payee 受領額の MerkleTree)。
        MerkleTree は個別 payee への包含証明発行（proof_for）に使う。
        """
        mandate = self._engine.get_mandate(mandate_id)

        receipts = self._engine.payee_receipts_cents(mandate_id, snapshot_at)
        total_settled = self._engine.settled_total_cents(mandate_id, snapshot_at)
        remaining = mandate.budget_cap.cents - total_settled
        escrow_balance = self._engine.escrow_balance_cents(mandate_id, snapshot_at)
        settlement_count = len(self._engine.settled_intent_ids(mandate_id, snapshot_at))

        # payee を account_id 昇順で並べて決定論的な葉順序にする。
        leaves = [
            MerkleLeaf(
                account_id=payee,
                balance_cents=receipts[payee],
                currency_code=mandate.currency.code,
            )
            for payee in sorted(receipts)
        ]
        if not leaves:
            leaves = [
                MerkleLeaf(
                    account_id=_EMPTY_LEAF_ID,
                    balance_cents=0,
                    currency_code=mandate.currency.code,
                )
            ]
        tree = MerkleTree(leaves)

        audit_root = GENESIS_HASH
        if self._engine.audit_log is not None:
            audit_root = self._engine.audit_log.latest_hash()

        proof = ProofOfMandate(
            snapshot_at=snapshot_at,
            mandate_id=mandate_id,
            principal_id=mandate.principal_id,
            currency_code=mandate.currency.code,
            budget_cap_cents=mandate.budget_cap.cents,
            total_settled_cents=total_settled,
            remaining_cents=remaining,
            settlement_count=settlement_count,
            payee_count=len(receipts),
            escrow_account_id=mandate.escrow_account_id,
            escrow_balance_cents=escrow_balance,
            is_within_budget=remaining >= 0,
            is_collateralized=escrow_balance >= remaining,
            payee_receipts_root=tree.root_hash,
            valid_from=mandate.valid_from,
            valid_until=mandate.valid_until,
            audit_log_root_hash=audit_root,
        )
        return proof, tree
