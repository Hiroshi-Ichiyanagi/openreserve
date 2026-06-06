"""
providers/base.py — ライセンスホルダー接続層の抽象インターフェース。

資金移動業者、信託銀行、銀行、暗号資産交換業者、海外送金プロバイダーを
すべて同じインターフェースで扱えるようにする。

新しいプロバイダーを追加する際は、LicensedProvider を継承して実装するだけで利用可能になる。

検証コアはこの抽象のうち `ProviderCategory` のみを参照する。具体的な送金実装
(モック / 本番アダプタ) は本パッケージには含めず、利用者側で差し替える。
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from openreserve.core.types import Currency, Money


class ProviderCategory(enum.Enum):
    """プロバイダーの法的区分。"""

    FUND_TRANSFER_TYPE_1 = "FUND_TRANSFER_TYPE_1"  # 第一種資金移動業（送金上限なし）
    FUND_TRANSFER_TYPE_2 = "FUND_TRANSFER_TYPE_2"  # 第二種資金移動業（1件100万円上限）
    FUND_TRANSFER_TYPE_3 = "FUND_TRANSFER_TYPE_3"  # 第三種資金移動業（1件5万円上限）
    BANK = "BANK"                                    # 銀行
    TRUST_BANK = "TRUST_BANK"                        # 信託銀行
    CRYPTO_EXCHANGE = "CRYPTO_EXCHANGE"              # 暗号資産交換業
    EM_INTERMEDIARY = "EM_INTERMEDIARY"              # 電子決済手段等取引業
    OVERSEAS_LICENSED = "OVERSEAS_LICENSED"          # 海外でライセンス取得済み（Wise等の日本法人）


class TransferStatus(enum.Enum):
    QUEUED = "QUEUED"           # 受付済み、未着手
    IN_PROGRESS = "IN_PROGRESS" # 処理中
    SETTLED = "SETTLED"         # 着金確認
    FAILED = "FAILED"           # 失敗
    CANCELED = "CANCELED"       # 取消
    HELD = "HELD"               # AMLレビュー保留


@dataclass(frozen=True)
class ProviderCapabilities:
    """プロバイダーの能力記述。RoutingEngine がこれを見て最適な経路を選ぶ。"""

    provider_id: str
    provider_name: str
    category: ProviderCategory
    supported_currencies: frozenset[Currency]
    supported_destination_countries: frozenset[str]  # ISO 3166-1 alpha-2
    per_transaction_limit_cents: dict[Currency, int]  # 通貨ごとの1件上限
    daily_limit_cents: dict[Currency, int]
    typical_settlement_seconds: int
    fee_basis_points: int  # bp = 0.01%
    fee_fixed_cents: dict[Currency, int]
    requires_kyc_level: int  # 0=不要, 1=簡易, 2=厳格
    supports_cancellation: bool
    supports_realtime_status: bool
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def can_handle(
        self,
        amount: Money,
        destination_country: str,
        kyc_level: int,
    ) -> tuple[bool, str | None]:
        """この取引をこのプロバイダーが処理できるか判定する。"""
        if amount.currency not in self.supported_currencies:
            return False, f"通貨 {amount.currency.code} 非対応"
        if destination_country not in self.supported_destination_countries:
            return False, f"国 {destination_country} 非対応"
        if amount.cents > self.per_transaction_limit_cents.get(amount.currency, 0):
            return False, f"取引上限超過: {amount} > 限度"
        if kyc_level < self.requires_kyc_level:
            return False, f"KYCレベル不足: 必要={self.requires_kyc_level}, 提供={kyc_level}"
        return True, None

    def estimate_fee(self, amount: Money) -> Money:
        """この取引の推定手数料。"""
        bps_fee = amount.cents * self.fee_basis_points // 10000
        fixed = self.fee_fixed_cents.get(amount.currency, 0)
        return Money(cents=bps_fee + fixed, currency=amount.currency)


@dataclass(frozen=True)
class TransferRequest:
    """プロバイダーへの送金依頼。"""

    request_id: str
    sender_account_ref: str  # プロバイダー側での送金元参照
    recipient_account_ref: str  # 受取側情報
    recipient_country: str
    amount: Money
    purpose_code: str
    travel_rule_payload: dict[str, Any] | None  # トラベルルール対応情報
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TransferReceipt:
    """送金受付の証憑。"""

    request_id: str
    provider_transaction_id: str
    accepted_at: datetime
    estimated_settlement_at: datetime | None
    status: TransferStatus
    fee_charged: Money
    cryptographic_proof: str | None  # 受領証拠ハッシュ


@dataclass(frozen=True)
class TransferStatusResponse:
    provider_transaction_id: str
    status: TransferStatus
    last_updated_at: datetime
    settlement_proof: str | None
    failure_reason: str | None


@dataclass(frozen=True)
class KYCPackage:
    """KYC情報パッケージ。"""

    user_id: str
    full_name: str
    date_of_birth: str  # ISO 8601
    nationality: str
    residence_country: str
    address_lines: tuple[str, ...]
    document_type: str  # PASSPORT, DRIVERS_LICENSE, MY_NUMBER_CARD, etc.
    document_number_hash: str  # 平文は保存しない
    additional_documents: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class KYCResult:
    user_id: str
    accepted: bool
    kyc_level_assigned: int
    rejection_reason: str | None
    decided_at: datetime


class LicensedProvider(ABC):
    """ライセンスホルダーの抽象基底クラス。

    実装側はこれを継承し、自社のライセンスとシステムに合わせて各メソッドを実装する。
    """

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def initiate_transfer(
        self, request: TransferRequest, *, at: datetime | None = None
    ) -> TransferReceipt:
        """送金を発行する。

        Args:
            request: 送金依頼。
            at: 受付時刻。シミュレーションでは仮想時刻を渡し、accepted_at /
                estimated_settlement_at をその時刻起点で算出させる。None の
                場合のみ実時刻 (datetime.now) を用いる。
        """
        ...

    @abstractmethod
    def query_status(self, provider_transaction_id: str) -> TransferStatusResponse: ...

    @abstractmethod
    def cancel_transfer(self, provider_transaction_id: str) -> bool: ...

    @abstractmethod
    def get_balance(self, account_ref: str, currency: Currency) -> Money: ...

    @abstractmethod
    def submit_kyc(self, package: KYCPackage) -> KYCResult: ...
