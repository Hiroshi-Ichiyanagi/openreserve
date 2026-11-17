"""
transparency/proofs.py — 3種の暗号学的証明の実装。

1. Proof of Reserves: 利用者負債総額 ≤ 準備資産総額 を証明
2. Proof of Solvency: ある時点で全顧客の同時償還を満たせる状態であることを証明
3. Proof of Compliance: 各取引のコンプライアンス判定根拠を改竄不可能に記録
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import Any

from openreserve.core.ledger import Ledger
from openreserve.core.types import Currency, OwnerType, TransactionStatus
from openreserve.transparency.audit_log import AuditLog
from openreserve.transparency.coverage import coverage_figures
from openreserve.transparency.merkle import MerkleLeaf, MerkleProof, MerkleTree
from openreserve.transparency.snapshot import collect_user_balances


def _pending_deprecation_message(old: str, new: str) -> str:
    return (
        f"{old} is the legacy reserve artifact and will be deprecated; use {new} "
        "(see docs/hypothetical/W_CHANGEOVER_DESIGN.md, section 9.3)"
    )


@dataclass(frozen=True)
class ProofOfReserves:
    """ある時点における準備資産証明。

    user_liabilities_root: 利用者負債（利用者口座残高）のMerkle Tree根ハッシュ
    user_liabilities_total_cents: 利用者負債合計
    reserve_assets_total_cents: 準備資産合計
    is_solvent: reserve >= liabilities

    Negative customer balances are never netted against liabilities: an account with a
    negative balance has no leaf and is left out of ``user_liabilities_total_cents``. Their
    number and total magnitude are reported in ``user_receivables_count`` and
    ``user_receivables_total_cents``. ``user_liabilities_count`` counts the accounts that
    have a leaf (zero-balance accounts keep theirs in this legacy artifact).

    ``coverage_ratio`` is ``Fraction(reserves, liabilities)``, or ``None`` when the
    liabilities are 0. In the summary, ``coverage_ratio_basis_points`` is
    ``(10000 * reserves) // liabilities``, or ``null`` when the liabilities are 0.

    Integers in the summary are JSON numbers, exact across implementations only up to
    2**53 - 1. The newer artifacts of ``transparency/coverage.py`` write every integer as a
    string. This class is kept for compatibility; its replacement is ``ReserveCoverage``.
    """

    snapshot_at: datetime
    currency: Currency
    user_liabilities_root: str
    user_liabilities_total_cents: int
    user_liabilities_count: int
    reserve_assets_total_cents: int
    reserve_accounts: tuple[str, ...]  # 準備資産口座のID一覧（公開）
    is_solvent: bool
    # Exact reserves / liabilities, or None when there are no liabilities (no float, no sentinel).
    coverage_ratio: Fraction | None
    user_receivables_count: int = 0  # customer accounts with a negative balance (no leaf)
    user_receivables_total_cents: int = 0  # sum of the magnitudes of those balances

    def to_public_summary(self) -> dict[str, Any]:
        """公開ダッシュボード用の要約。利用者個別残高は含まない。"""
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "currency": self.currency.code,
            "user_liabilities_merkle_root": self.user_liabilities_root,
            "user_liabilities_total": self.user_liabilities_total_cents,
            "user_count": self.user_liabilities_count,
            "reserve_assets_total": self.reserve_assets_total_cents,
            "reserve_accounts": list(self.reserve_accounts),
            "is_solvent": self.is_solvent,
            "coverage_ratio_basis_points": coverage_figures(
                self.reserve_assets_total_cents, self.user_liabilities_total_cents
            ).basis_points_floor,
            "user_receivables_count": self.user_receivables_count,
            "user_receivables_total": self.user_receivables_total_cents,
        }


class ProofOfReservesGenerator:
    """元帳から Proof of Reserves を生成する。

    Legacy generator, kept with its signature and artifact for a deprecation period. Calling
    :meth:`generate` emits a ``PendingDeprecationWarning`` naming the replacement,
    ``ReserveCoverageGenerator``.
    """

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def generate(
        self,
        currency: Currency,
        snapshot_at: datetime,
    ) -> tuple[ProofOfReserves, MerkleTree]:
        """See :meth:`_generate`; this entry point also emits the pending deprecation warning."""
        warnings.warn(
            _pending_deprecation_message("ProofOfReservesGenerator.generate", "ReserveCoverageGenerator.generate"),
            PendingDeprecationWarning,
            stacklevel=2,
        )
        return self._generate(currency, snapshot_at)

    def _generate(
        self,
        currency: Currency,
        snapshot_at: datetime,
    ) -> tuple[ProofOfReserves, MerkleTree]:
        """指定通貨について、snapshot_at 時点の Proof of Reserves を生成する。

        snapshot_at は必須。wall-clock (datetime.now()) フォールバックは proof
        artifact の決定論性を損なうため持たない。ライブ dashboard 表示は呼び出し側が
        datetime.now() を明示的に渡すこと。

        Returns: (ProofOfReserves, 利用者残高のMerkleTree)
        MerkleTreeは個別利用者へのProof発行に使う。
        """
        # 利用者口座の残高を集計
        # A negative balance is a receivable, not a negative liability: it gets no leaf and
        # is never netted against what is owed to other customers.
        leaves: list[MerkleLeaf] = []
        total_liabilities = 0
        receivables_count = 0
        receivables_total = 0
        for account_id, balance_cents in collect_user_balances(self._ledger, currency, snapshot_at):
            if balance_cents < 0:
                receivables_count += 1
                receivables_total += -balance_cents
                continue
            leaves.append(
                MerkleLeaf(
                    account_id=account_id,
                    balance_cents=balance_cents,
                    currency_code=currency.code,
                )
            )
            total_liabilities += balance_cents

        if not leaves:
            # 利用者口座が0件でも空のProofを生成可能にする
            leaves = [
                MerkleLeaf(account_id="__empty__", balance_cents=0, currency_code=currency.code)
            ]
            total_liabilities = 0

        tree = MerkleTree(leaves)

        # 準備資産口座の残高を集計
        reserve_accounts = self._ledger.list_accounts(owner_type=OwnerType.RESERVE)
        total_reserves = 0
        reserve_account_ids: list[str] = []
        for account in reserve_accounts:
            if account.currency != currency:
                continue
            reserve_account_ids.append(account.account_id)
            balance = self._ledger.balance(account.account_id, as_of=snapshot_at)
            total_reserves += balance.cents

        figures = coverage_figures(total_reserves, total_liabilities)
        is_solvent = figures.covered

        proof = ProofOfReserves(
            snapshot_at=snapshot_at,
            currency=currency,
            user_liabilities_root=tree.root_hash,
            user_liabilities_total_cents=total_liabilities,
            user_liabilities_count=len([leaf for leaf in leaves if leaf.account_id != "__empty__"]),
            reserve_assets_total_cents=total_reserves,
            reserve_accounts=tuple(reserve_account_ids),
            is_solvent=is_solvent,
            coverage_ratio=figures.ratio,
            user_receivables_count=receivables_count,
            user_receivables_total_cents=receivables_total,
        )
        return proof, tree


@dataclass(frozen=True)
class ProofOfSolvency:
    """支払能力証明。複数通貨をまたぐ全社の財務健全性を示す。

    各通貨ごとの ProofOfReserves と、それらの集約結果。
    """

    snapshot_at: datetime
    per_currency: tuple[ProofOfReserves, ...]
    overall_solvent: bool
    audit_log_root_hash: str  # 透明性ログとの連結証明

    def to_public_summary(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "currencies": [p.to_public_summary() for p in self.per_currency],
            "overall_solvent": self.overall_solvent,
            "audit_log_committed": self.audit_log_root_hash,
        }


class ProofOfSolvencyGenerator:
    """全通貨を統合した Proof of Solvency を生成する。

    Legacy generator, kept with its signature and artifact for a deprecation period. Calling
    :meth:`generate` emits a ``PendingDeprecationWarning`` naming the replacement,
    ``DeclaredReserveCoverageGenerator``.
    """

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._por_generator = ProofOfReservesGenerator(ledger)

    def generate(
        self,
        currencies: tuple[Currency, ...],
        audit_log_root_hash: str,
        snapshot_at: datetime,
    ) -> tuple[ProofOfSolvency, dict[Currency, MerkleTree]]:
        # snapshot_at は必須 (wall-clock フォールバックなし)。
        warnings.warn(
            _pending_deprecation_message(
                "ProofOfSolvencyGenerator.generate", "DeclaredReserveCoverageGenerator.generate"
            ),
            PendingDeprecationWarning,
            stacklevel=2,
        )
        per_currency_proofs: list[ProofOfReserves] = []
        trees: dict[Currency, MerkleTree] = {}
        all_solvent = True
        for currency in currencies:
            por, tree = self._por_generator._generate(currency=currency, snapshot_at=snapshot_at)
            per_currency_proofs.append(por)
            trees[currency] = tree
            if not por.is_solvent:
                all_solvent = False

        proof = ProofOfSolvency(
            snapshot_at=snapshot_at,
            per_currency=tuple(per_currency_proofs),
            overall_solvent=all_solvent,
            audit_log_root_hash=audit_log_root_hash,
        )
        return proof, trees


@dataclass(frozen=True)
class ComplianceReport:
    """ある期間のコンプライアンス活動の集約レポート。

    個別取引情報は伏せたまま、判定の統計と判定エンジンのバージョンを公開する。
    規制当局への定期報告と、利用者への透明性ダッシュボードに使う。
    """

    period_start: datetime
    period_end: datetime
    total_transactions: int
    approved: int
    rejected: int
    review_required: int
    high_risk_transactions: int  # risk_score >= 70
    rule_versions_used: tuple[str, ...]
    triggered_rule_counts: tuple[tuple[str, int], ...]  # ((rule, count), ...)

    def to_public_summary(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "total_transactions": self.total_transactions,
            "decisions": {
                "approved": self.approved,
                "rejected": self.rejected,
                "review_required": self.review_required,
            },
            "high_risk_count": self.high_risk_transactions,
            "rule_versions": list(self.rule_versions_used),
            "triggered_rules": dict(self.triggered_rule_counts),
        }


class ProofOfComplianceGenerator:
    """元帳のトランザクション履歴から Compliance Report を生成する。

    audit_log を渡すと、ledger に tx を作らない経路で発生した自動 REJECTED
    (aml_auto_rejected イベント) も集計に含める。これにより規制当局視点で
    「AML が拒否した取引数」 が ledger 走査だけでは欠落しない (透明性 invariant)。
    """

    def __init__(self, ledger: Ledger, audit_log: AuditLog | None = None) -> None:
        self._ledger = ledger
        self._audit_log = audit_log

    def generate(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> ComplianceReport:
        total = 0
        approved = 0
        rejected = 0
        review = 0
        high_risk = 0
        rule_versions: set[str] = set()
        triggered_counts: dict[str, int] = {}

        for tx in self._ledger.iter_all_transactions():
            if tx.initiated_at < period_start or tx.initiated_at > period_end:
                continue
            if tx.status == TransactionStatus.REVERSED:
                continue
            total += 1

            if tx.compliance_decision is None:
                continue

            decision = tx.compliance_decision
            rule_versions.add(decision.rule_version)

            if decision.decision == "APPROVED":
                approved += 1
            elif decision.decision == "REJECTED":
                rejected += 1
            elif decision.decision == "REVIEW_REQUIRED":
                review += 1

            if decision.risk_score >= 70:
                high_risk += 1

            for rule in decision.triggered_rules:
                triggered_counts[rule] = triggered_counts.get(rule, 0) + 1

        # 自動 REJECTED (元帳 tx を作らない経路) を audit_log から補完する。
        # bridge.ingest_revenue が AML 評価で REJECTED と判定すると return None
        # するため、ledger 走査だけでは取り漏らす。aml_auto_rejected は
        # engine/ledger_bridge.py で生成される append-only イベント。
        #
        # 時間フィルタは payload["occurred_at"] (シミュレーション/呼び出し側
        # 時刻) を使う。ledger 走査が tx.initiated_at を使うのと意味論を揃える
        # (evt.timestamp は wall-clock で AuditLog.append が決めるため、シナリオ
        # 時刻と異なる場合がある)。
        #
        # 安全性 invariant (Phase B B3): audit_log を信頼して集計する前に
        # 必ず verify_chain() を呼ぶ。改竄された audit_log を「信頼できる
        # ログから集計したから真」 と取り違えないようにする。改竄検知時は
        # 例外を上げて呼び出し側に異常を通知する (silently 集計しない)。
        if self._audit_log is not None:
            is_valid, err = self._audit_log.verify_chain()
            if not is_valid:
                raise ValueError(
                    f"audit_log integrity check failed; refusing to generate "
                    f"compliance report from tampered chain: {err}"
                )
            for evt in self._audit_log.iter_events():
                if evt.event_type != "aml_auto_rejected":
                    continue
                occurred_raw = evt.payload.get("occurred_at")
                if not isinstance(occurred_raw, str):
                    continue  # payload 形式不正は安全側で skip (集計汚染を防ぐ)
                try:
                    occurred = datetime.fromisoformat(occurred_raw)
                except ValueError:
                    continue
                if occurred < period_start or occurred > period_end:
                    continue
                total += 1
                rejected += 1
                # 自動 REJECTED は risk_score >= reject_threshold (>= 70) が
                # 必須条件。high_risk 集計対象。
                risk = evt.payload.get("risk_score")
                if isinstance(risk, int) and risk >= 70:
                    high_risk += 1
                rule_ver = evt.payload.get("rule_version")
                if isinstance(rule_ver, str):
                    rule_versions.add(rule_ver)
                for rule in evt.payload.get("triggered_rules", []) or ():
                    triggered_counts[rule] = triggered_counts.get(rule, 0) + 1

        sorted_rules = tuple(sorted(triggered_counts.items(), key=lambda x: -x[1]))

        return ComplianceReport(
            period_start=period_start,
            period_end=period_end,
            total_transactions=total,
            approved=approved,
            rejected=rejected,
            review_required=review,
            high_risk_transactions=high_risk,
            rule_versions_used=tuple(sorted(rule_versions)),
            triggered_rule_counts=sorted_rules,
        )
