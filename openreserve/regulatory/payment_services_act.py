"""
regulatory/payment_services_act.py — 資金決済法（資決法）コンプライアンス層。

第一種〜第三種資金移動業の各種規制要件をシステムに強制する：

- 滞留規制（第一種）：送金目的を超える資金滞留の禁止
- 送金上限（第二種：1件100万円、第三種：1件5万円）
- 履行保証金の供託金額計算
- 未達債務管理（送金処理中・未達の負債）

これらは元帳のトランザクションから自動的に算出され、
当局への定期報告と内部リスク管理の両方に使われる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from openreserve.core.ledger import Ledger
from openreserve.core.types import Currency, Money, OwnerType, Transaction, TransactionStatus
from openreserve.providers.base import ProviderCategory


@dataclass(frozen=True)
class FundRetentionAnalysis:
    """滞留規制分析結果（第一種資金移動業）。

    第一種資金移動業者は「送金目的を超える資金の滞留」を禁止される。
    具体的には、利用者からの資金が「送金途中」以外の状態で長期間残留すると違反。
    """

    snapshot_at: datetime
    user_account_count: int
    total_user_balance_cents: int
    long_retention_count: int  # 7日超滞留している利用者数
    long_retention_amount_cents: int
    average_retention_days: float
    flagged_accounts: tuple[str, ...]
    is_compliant: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "user_account_count": self.user_account_count,
            "total_user_balance_cents": self.total_user_balance_cents,
            "long_retention": {
                "count": self.long_retention_count,
                "amount_cents": self.long_retention_amount_cents,
            },
            "average_retention_days": round(self.average_retention_days, 2),
            "flagged_accounts": list(self.flagged_accounts),
            "is_compliant": self.is_compliant,
        }


@dataclass(frozen=True)
class TransactionLimitViolation:
    """送金上限違反の記録。"""

    transaction_id: str
    occurred_at: datetime
    provider_category: str
    amount_cents: int
    limit_cents: int
    description: str


@dataclass(frozen=True)
class ReserveDepositCalculation:
    """履行保証金の供託金額計算。

    第二種・第三種資金移動業者は、未達債務に応じた供託または保全契約が必要。
    未達債務 = 利用者からの預り金のうち、まだ送金が完了していない金額。
    """

    snapshot_at: datetime
    currency: Currency
    pending_obligations_cents: int  # 未達債務（送金処理中の債務）
    settled_buffer_cents: int  # 既決済済みの利用者預り金
    required_deposit_cents: int  # 法定要件に基づく必要供託額
    actual_reserve_cents: int  # 実際の準備資産
    is_compliant: bool
    deficit_cents: int  # 不足額（マイナスは余剰）

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_at": self.snapshot_at.isoformat(),
            "currency": self.currency.code,
            "pending_obligations_cents": self.pending_obligations_cents,
            "settled_buffer_cents": self.settled_buffer_cents,
            "required_deposit_cents": self.required_deposit_cents,
            "actual_reserve_cents": self.actual_reserve_cents,
            "is_compliant": self.is_compliant,
            "deficit_cents": self.deficit_cents,
        }


class PaymentServicesActCompliance:
    """資金決済法コンプライアンスチェッカー。"""

    # 滞留判定の閾値（日数）
    LONG_RETENTION_THRESHOLD_DAYS = 7

    # 法定供託率（資金決済法第43条相当：未達債務の100%以上を保全）
    REQUIRED_DEPOSIT_RATIO_BPS = 10000  # 100% = 10000 bps

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def analyze_fund_retention(
        self,
        snapshot_at: datetime | None = None,
    ) -> FundRetentionAnalysis:
        """利用者口座の資金滞留状況を分析する（第一種規制）。"""
        snapshot_at = snapshot_at or datetime.now(timezone.utc)
        threshold_date = snapshot_at - timedelta(days=self.LONG_RETENTION_THRESHOLD_DAYS)

        user_accounts = self._ledger.list_accounts(owner_type=OwnerType.USER)
        total_balance_cents = 0
        long_retention_count = 0
        long_retention_amount = 0
        retention_days_sum = 0.0
        flagged: list[str] = []

        for account in user_accounts:
            balance = self._ledger.balance(account.account_id, as_of=snapshot_at)
            if balance.cents <= 0:
                continue
            total_balance_cents += balance.cents

            # 最終入金日時を取得（最新の正のエントリー）
            last_deposit_at = self._find_last_deposit_at(account.account_id, snapshot_at)
            if last_deposit_at is None:
                continue
            retention_days = (snapshot_at - last_deposit_at).total_seconds() / 86400
            retention_days_sum += retention_days

            if last_deposit_at < threshold_date:
                long_retention_count += 1
                long_retention_amount += balance.cents
                flagged.append(account.account_id)

        avg_retention = (
            retention_days_sum / len(user_accounts) if user_accounts else 0.0
        )

        return FundRetentionAnalysis(
            snapshot_at=snapshot_at,
            user_account_count=len(user_accounts),
            total_user_balance_cents=total_balance_cents,
            long_retention_count=long_retention_count,
            long_retention_amount_cents=long_retention_amount,
            average_retention_days=avg_retention,
            flagged_accounts=tuple(flagged),
            is_compliant=long_retention_count == 0,
        )

    def check_transaction_limits(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> list[TransactionLimitViolation]:
        """期間内の送金で、各プロバイダー区分の上限を超えたものを検出する。"""
        violations: list[TransactionLimitViolation] = []

        for tx in self._ledger.iter_all_transactions():
            if tx.initiated_at < period_start or tx.initiated_at > period_end:
                continue

            metadata = dict(tx.metadata)
            provider_category_str = metadata.get("provider_category")
            if not provider_category_str:
                continue

            # 主要金額（最大の正のエントリー）を取得
            positive_entries = [e for e in tx.entries if e.amount.is_positive()]
            if not positive_entries:
                continue
            main_amount = max(positive_entries, key=lambda e: e.amount.cents).amount

            limit_cents = self._get_limit_cents(provider_category_str, main_amount.currency)
            if limit_cents is None:
                continue

            if main_amount.cents > limit_cents:
                violations.append(
                    TransactionLimitViolation(
                        transaction_id=tx.transaction_id,
                        occurred_at=tx.initiated_at,
                        provider_category=provider_category_str,
                        amount_cents=main_amount.cents,
                        limit_cents=limit_cents,
                        description=(
                            f"{provider_category_str}: {main_amount.cents}cents > {limit_cents}cents 上限超過"
                        ),
                    )
                )

        return violations

    def calculate_required_reserve(
        self,
        currency: Currency,
        snapshot_at: datetime | None = None,
    ) -> ReserveDepositCalculation:
        """指定通貨について、法定供託額と実準備額の対比を計算する。"""
        snapshot_at = snapshot_at or datetime.now(timezone.utc)

        # 未達債務 = PENDING状態のトランザクションの合計
        pending_obligations = 0
        settled_buffer = 0

        # 利用者口座のSETTLED残高合計（既決済の預り金）
        # G-4: 負残高 (creator が既に引き出した状態) は預り金ではないので除外
        for account in self._ledger.list_accounts(owner_type=OwnerType.USER):
            if account.currency != currency:
                continue
            balance = self._ledger.balance(account.account_id, as_of=snapshot_at)
            if balance.cents > 0:
                settled_buffer += balance.cents

        # PENDING状態の取引による未達債務
        # G-4: snapshot_at 時点で PENDING だった取引を拾う (現状の status だけ見ない)
        # 条件: initiated_at <= snapshot_at かつ (settled_at が None または settled_at > snapshot_at)
        for tx in self._ledger.iter_all_transactions():
            if tx.initiated_at > snapshot_at:
                continue
            # snapshot_at 時点での PENDING 判定
            was_pending_at_snapshot = (
                tx.settled_at is None or tx.settled_at > snapshot_at
            )
            if not was_pending_at_snapshot:
                continue

            # G-4: transaction_type を取得 (PAYOUT判定用)
            metadata = dict(tx.metadata)
            is_payout = metadata.get("transaction_type") == "PAYOUT"

            for entry in tx.entries:
                if entry.amount.currency != currency:
                    continue
                if entry.amount.is_positive():  # 受取側
                    try:
                        account = self._ledger.get_account(entry.account_id)
                        if account.owner_type == OwnerType.USER:
                            pending_obligations += entry.amount.cents
                    except Exception:
                        pass
                elif is_payout and entry.amount.is_negative():
                    # G-4 追加: PAYOUT-PENDING の出金側
                    # creator USER口座から出ていく送金途中の資金は未達債務
                    try:
                        account = self._ledger.get_account(entry.account_id)
                        if account.owner_type == OwnerType.USER:
                            pending_obligations += abs(entry.amount.cents)
                    except Exception:
                        pass

        # 法定供託額 = (未達債務 + 利用者預り金) × 100%
        total_obligations = pending_obligations + settled_buffer
        required_deposit = total_obligations * self.REQUIRED_DEPOSIT_RATIO_BPS // 10000

        # 実際の準備資産
        actual_reserve = 0
        for account in self._ledger.list_accounts(owner_type=OwnerType.RESERVE):
            if account.currency != currency:
                continue
            actual_reserve += self._ledger.balance(account.account_id, as_of=snapshot_at).cents

        deficit = required_deposit - actual_reserve

        return ReserveDepositCalculation(
            snapshot_at=snapshot_at,
            currency=currency,
            pending_obligations_cents=pending_obligations,
            settled_buffer_cents=settled_buffer,
            required_deposit_cents=required_deposit,
            actual_reserve_cents=actual_reserve,
            is_compliant=deficit <= 0,
            deficit_cents=deficit,
        )

    # ---------- 内部ヘルパー ----------

    def _find_last_deposit_at(
        self,
        account_id: str,
        as_of: datetime,
    ) -> datetime | None:
        """口座への最後の入金（正のエントリー）の発生時刻を返す。"""
        last_at: datetime | None = None
        for tx in self._ledger.iter_all_transactions():
            if tx.initiated_at > as_of:
                continue
            if tx.status not in (TransactionStatus.SETTLED, TransactionStatus.PENDING):
                continue
            for entry in tx.entries:
                if entry.account_id == account_id and entry.amount.is_positive():
                    if last_at is None or tx.initiated_at > last_at:
                        last_at = tx.initiated_at
        return last_at

    def _get_limit_cents(
        self,
        provider_category: str,
        currency: Currency,
    ) -> int | None:
        """プロバイダー区分と通貨から、1件あたりの法定上限を返す。"""
        # JPY基準。外貨は外為法レートで換算が必要だが簡易化
        if currency != Currency.JPY:
            return None
        if provider_category == ProviderCategory.FUND_TRANSFER_TYPE_2.value:
            return 1_000_000  # 100万円
        if provider_category == ProviderCategory.FUND_TRANSFER_TYPE_3.value:
            return 50_000  # 5万円
        return None  # 第一種は無制限
