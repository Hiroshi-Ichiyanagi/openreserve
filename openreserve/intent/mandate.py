"""
intent/mandate.py — インテント（意図）ベースの自律決済オーソライゼーション層。

x402 が「都度、成果を測って決済」するのに対し、本モジュールは
**「利用者が大枠の意図（予算上限・許可用途・有効期間）をあらかじめ信託し、
その枠内で自律エージェントが最適な決済を繰り返す」** モデルを扱う。
ERC-4337（口座抽象化）／セッション鍵の「事前デポジット + 枠内自動執行」を、
オンチェーン実行ではなく **検証コア側**（元帳会計 + 証明）として実装する。

信頼の要は「枠を一度も超えていないこと」を第三者が後から検証できることにある。
そのため本エンジンは：

1. Mandate（委任枠）を完全担保（escrow 残高 >= budget_cap）で成立させる。
2. 各インテントの決済可否を枠のルール（有効期間・用途・1件上限・累計予算）で判定する。
3. 成立・却下のすべての判断を、改竄検出可能な監査ログ（ハッシュチェーン）に刻む。
4. 累計消化額は必ず元帳から再計算する（別カウンタを信頼しない）。

決済実行（オンチェーン bundler / paymaster / セッション鍵）は本パッケージの範囲外。
ここが担保するのは「枠内で正しく決済され、それをオフラインで検証できる」ことである。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.types import Currency, Money, TransactionStatus
from openreserve.transparency.audit_log import AuditLog

# 元帳メタデータのキー / 値（累計消化を元帳から再構成するためのタグ）
_MANDATE_ID_KEY = "mandate_id"
_INTENT_ID_KEY = "intent_id"
_TXN_TYPE_KEY = "transaction_type"
_INTENT_SETTLEMENT = "INTENT_SETTLEMENT"


class MandateError(Exception):
    """委任枠の構成が不正なときに送出される（呼び出し側のバグ）。"""


@dataclass(frozen=True)
class Mandate:
    """自律エージェントに与える「意図（予算枠）」の委任。

    principal（委任者）が escrow 口座に budget_cap を預け入れ、その枠内で
    自律エージェントが決済することを許可する。ルールはすべて明示値で保持し、
    ウォールクロックには依存しない（証明の決定論性を保つため）。

    budget_cap:            枠の上限（累計消化がこれを超えない）。
    per_transaction_limit: 1 件あたりの上限（None なら 1 件上限なし、枠上限のみ）。
    allowed_purposes:      許可する purpose_code の集合。
    valid_from/valid_until: 有効期間（両端含む）。
    escrow_account_id:     枠を裏付ける資金を保持する口座（完全担保）。
    """

    mandate_id: str
    principal_id: str
    escrow_account_id: str
    currency: Currency
    budget_cap: Money
    allowed_purposes: frozenset[str]
    valid_from: datetime
    valid_until: datetime
    created_at: datetime
    per_transaction_limit: Money | None = None

    def __post_init__(self) -> None:
        if self.budget_cap.currency != self.currency:
            raise MandateError(
                f"budget_cap currency {self.budget_cap.currency.code} "
                f"!= mandate currency {self.currency.code}"
            )
        if not self.budget_cap.is_positive():
            raise MandateError("budget_cap must be positive")
        if self.per_transaction_limit is not None:
            if self.per_transaction_limit.currency != self.currency:
                raise MandateError("per_transaction_limit currency mismatch")
            if not self.per_transaction_limit.is_positive():
                raise MandateError("per_transaction_limit must be positive")
        if self.valid_until < self.valid_from:
            raise MandateError("valid_until must be >= valid_from")
        if not self.allowed_purposes:
            raise MandateError("allowed_purposes must not be empty")

    def check_static(
        self, purpose: str, amount: Money, at: datetime
    ) -> tuple[bool, str]:
        """累計に依存しない静的ルール（有効期間・用途・1件上限）を判定する。

        累計予算（budget_cap）と重複チェックはエンジン側（元帳参照が必要）で行う。
        Returns: (ok, reason)。ok=True のとき reason="OK"。
        """
        if amount.currency != self.currency:
            return False, "CURRENCY_MISMATCH"
        if not amount.is_positive():
            return False, "NON_POSITIVE_AMOUNT"
        if at < self.valid_from or at > self.valid_until:
            return False, "OUTSIDE_WINDOW"
        if purpose not in self.allowed_purposes:
            return False, "PURPOSE_NOT_ALLOWED"
        if self.per_transaction_limit is not None and amount > self.per_transaction_limit:
            return False, "PER_TX_LIMIT_EXCEEDED"
        return True, "OK"


@dataclass(frozen=True)
class IntentSettlementResult:
    """1 件のインテント決済判断（成立 or 却下）の記録。

    decision="SETTLED" のとき transaction_id が非 None。
    decision="DENIED" のとき reason に却下理由コードが入る（元帳 tx は作られない）。
    """

    intent_id: str
    mandate_id: str
    decision: str  # "SETTLED" | "DENIED"
    amount: Money
    purpose: str
    payee_account_id: str
    reason: str
    decided_at: datetime
    remaining_after_cents: int
    transaction_id: str | None = None
    audit_sequence: int | None = None

    @property
    def is_settled(self) -> bool:
        return self.decision == "SETTLED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "mandate_id": self.mandate_id,
            "decision": self.decision,
            "amount_cents": self.amount.cents,
            "currency": self.amount.currency.code,
            "purpose": self.purpose,
            "payee_account_id": self.payee_account_id,
            "reason": self.reason,
            "decided_at": self.decided_at.isoformat(),
            "remaining_after_cents": self.remaining_after_cents,
            "transaction_id": self.transaction_id,
            "audit_sequence": self.audit_sequence,
        }


class IntentSettlementEngine:
    """委任枠に基づいてインテントを自律決済するエンジン。

    元帳（Ledger）の上に構築され、決済は escrow -> payee の複式仕訳として記録される。
    監査ログ（AuditLog）を渡すと、成立・却下のすべての判断がハッシュチェーンに刻まれ、
    「枠を一度も超えていない」ことを規制当局・利用者・取引相手が後から検証できる。

    累計消化額は常に元帳から再構成する（メタデータ tag で識別）。別カウンタを持たない
    ことで、カウンタと元帳の乖離という一類の不整合を構造的に排除する。
    """

    def __init__(self, ledger: Ledger, audit_log: AuditLog | None = None) -> None:
        self._ledger = ledger
        self._audit_log = audit_log
        self._mandates: dict[str, Mandate] = {}

    @property
    def audit_log(self) -> AuditLog | None:
        """接続された監査ログ（証明のコミットメントに使う）。未接続なら None。"""
        return self._audit_log

    # ---------- 委任枠の作成 ----------

    def create_mandate(
        self,
        *,
        mandate_id: str,
        principal_id: str,
        escrow_account_id: str,
        budget_cap: Money,
        allowed_purposes: frozenset[str],
        valid_from: datetime,
        valid_until: datetime,
        created_at: datetime,
        per_transaction_limit: Money | None = None,
    ) -> Mandate:
        """委任枠を成立させる。

        成立条件：escrow 口座が budget_cap 以上で資金化済みであること（完全担保）。
        「先にコントラクトへデポジット、以後は枠内で自動執行」を会計として強制する。
        """
        if mandate_id in self._mandates:
            raise MandateError(f"mandate already exists: {mandate_id}")

        escrow = self._ledger.get_account(escrow_account_id)
        if escrow.currency != budget_cap.currency:
            raise MandateError(
                f"escrow currency {escrow.currency.code} "
                f"!= budget_cap currency {budget_cap.currency.code}"
            )

        escrow_balance = self._ledger.balance(escrow_account_id, as_of=created_at)
        if escrow_balance < budget_cap:
            raise MandateError(
                f"under-collateralized mandate: escrow balance {escrow_balance} "
                f"< budget_cap {budget_cap}. Fund the escrow before creating the mandate."
            )

        mandate = Mandate(
            mandate_id=mandate_id,
            principal_id=principal_id,
            escrow_account_id=escrow_account_id,
            currency=budget_cap.currency,
            budget_cap=budget_cap,
            allowed_purposes=frozenset(allowed_purposes),
            valid_from=valid_from,
            valid_until=valid_until,
            created_at=created_at,
            per_transaction_limit=per_transaction_limit,
        )
        self._mandates[mandate_id] = mandate

        self._audit(
            "mandate_created",
            {
                _MANDATE_ID_KEY: mandate_id,
                "principal_id": principal_id,
                "escrow_account_id": escrow_account_id,
                "currency": mandate.currency.code,
                "budget_cap_cents": budget_cap.cents,
                "per_transaction_limit_cents": (
                    per_transaction_limit.cents if per_transaction_limit else None
                ),
                "allowed_purposes": sorted(mandate.allowed_purposes),
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
            },
            at=created_at,
        )
        return mandate

    def get_mandate(self, mandate_id: str) -> Mandate:
        try:
            return self._mandates[mandate_id]
        except KeyError:
            raise MandateError(f"unknown mandate: {mandate_id}") from None

    # ---------- 消化状況（元帳から再構成） ----------

    def settled_total_cents(self, mandate_id: str, as_of: datetime) -> int:
        """as_of 時点までに mandate で成立した累計消化額（cents）。"""
        mandate = self.get_mandate(mandate_id)
        return sum(amt for _tx_id, _intent, amt, _payee in self._iter_settlements(mandate, as_of))

    def remaining_cents(self, mandate_id: str, as_of: datetime) -> int:
        """as_of 時点での残枠（cents）= budget_cap - 累計消化。"""
        mandate = self.get_mandate(mandate_id)
        return mandate.budget_cap.cents - self.settled_total_cents(mandate_id, as_of)

    def settled_intent_ids(self, mandate_id: str, as_of: datetime) -> set[str]:
        """as_of 時点までに成立済みのインテント ID 集合（冪等性判定に使う）。"""
        mandate = self.get_mandate(mandate_id)
        return {intent for _tx, intent, _amt, _payee in self._iter_settlements(mandate, as_of)}

    def payee_receipts_cents(self, mandate_id: str, as_of: datetime) -> dict[str, int]:
        """as_of 時点での受取人（payee）別の受領累計（cents）。証明の Merkle 葉に使う。"""
        mandate = self.get_mandate(mandate_id)
        receipts: dict[str, int] = {}
        for _tx, _intent, amt, payee in self._iter_settlements(mandate, as_of):
            receipts[payee] = receipts.get(payee, 0) + amt
        return receipts

    def escrow_balance_cents(self, mandate_id: str, as_of: datetime) -> int:
        """as_of 時点での escrow 口座残高（cents）。担保状況の検証に使う。"""
        mandate = self.get_mandate(mandate_id)
        return self._ledger.balance(mandate.escrow_account_id, as_of=as_of).cents

    # ---------- インテント決済 ----------

    def settle_intent(
        self,
        *,
        mandate_id: str,
        intent_id: str,
        payee_account_id: str,
        amount: Money,
        purpose: str,
        at: datetime,
    ) -> IntentSettlementResult:
        """1 件のインテントを枠に照らして自律決済する。

        枠内なら escrow -> payee の複式仕訳を SETTLED で記録し、監査ログに intent_settled
        を刻む。枠を外れるなら元帳には一切書かず、監査ログに intent_denied を刻んで
        却下を返す。累計予算は元帳から再構成した値で判定する。
        """
        mandate = self.get_mandate(mandate_id)

        # 静的ルール（有効期間・用途・1件上限・通貨・符号）
        ok, reason = mandate.check_static(purpose, amount, at)

        # 冪等性：同一 intent_id の二重成立を防ぐ
        if ok and intent_id in self.settled_intent_ids(mandate_id, at):
            ok, reason = False, "DUPLICATE_INTENT"

        # 累計予算：残枠を超えないこと
        remaining_before = self.remaining_cents(mandate_id, at)
        if ok and amount.cents > remaining_before:
            ok, reason = False, "BUDGET_EXCEEDED"

        if not ok:
            evt = self._audit(
                "intent_denied",
                {
                    _MANDATE_ID_KEY: mandate_id,
                    _INTENT_ID_KEY: intent_id,
                    "payee_account_id": payee_account_id,
                    "amount_cents": amount.cents,
                    "currency": amount.currency.code,
                    "purpose": purpose,
                    "reason": reason,
                    "at": at.isoformat(),
                    "remaining_cents": remaining_before,
                },
                at=at,
            )
            return IntentSettlementResult(
                intent_id=intent_id,
                mandate_id=mandate_id,
                decision="DENIED",
                amount=amount,
                purpose=purpose,
                payee_account_id=payee_account_id,
                reason=reason,
                decided_at=at,
                remaining_after_cents=remaining_before,
                transaction_id=None,
                audit_sequence=evt.sequence if evt else None,
            )

        # 枠内 → escrow -> payee を複式仕訳で記録（SETTLED）
        builder = TransactionBuilder(
            purpose_code=purpose, initiator_id=mandate.principal_id, initiated_at=at
        )
        builder.transfer(mandate.escrow_account_id, payee_account_id, amount)
        builder.with_metadata(_TXN_TYPE_KEY, _INTENT_SETTLEMENT)
        builder.with_metadata(_MANDATE_ID_KEY, mandate_id)
        builder.with_metadata(_INTENT_ID_KEY, intent_id)
        tx = builder.build()
        self._ledger.post(tx)
        self._ledger.settle(tx.transaction_id, settled_at=at)

        remaining_after = remaining_before - amount.cents
        evt = self._audit(
            "intent_settled",
            {
                _MANDATE_ID_KEY: mandate_id,
                _INTENT_ID_KEY: intent_id,
                "payee_account_id": payee_account_id,
                "amount_cents": amount.cents,
                "currency": amount.currency.code,
                "purpose": purpose,
                "transaction_id": tx.transaction_id,
                "settled_at": at.isoformat(),
                "remaining_after_cents": remaining_after,
            },
            at=at,
        )
        return IntentSettlementResult(
            intent_id=intent_id,
            mandate_id=mandate_id,
            decision="SETTLED",
            amount=amount,
            purpose=purpose,
            payee_account_id=payee_account_id,
            reason="OK",
            decided_at=at,
            remaining_after_cents=remaining_after,
            transaction_id=tx.transaction_id,
            audit_sequence=evt.sequence if evt else None,
        )

    # ---------- 内部ヘルパー ----------

    def _iter_settlements(
        self, mandate: Mandate, as_of: datetime
    ) -> Iterator[tuple[str, str, int, str]]:
        """mandate に紐づく as_of 時点までの SETTLED 決済を列挙する。

        Yields: (transaction_id, intent_id, amount_cents, payee_account_id)。
        金額は escrow からの流出額（正の cents）。
        """
        for tx in self._ledger.iter_all_transactions():
            if tx.status != TransactionStatus.SETTLED:
                continue
            if tx.settled_at is None or tx.settled_at > as_of:
                continue
            meta = dict(tx.metadata)
            if meta.get(_TXN_TYPE_KEY) != _INTENT_SETTLEMENT:
                continue
            if meta.get(_MANDATE_ID_KEY) != mandate.mandate_id:
                continue
            intent_id = meta.get(_INTENT_ID_KEY, "")
            outflow = 0
            payee = ""
            for entry in tx.entries:
                if entry.account_id == mandate.escrow_account_id and entry.amount.is_negative():
                    outflow += -entry.amount.cents
                elif entry.amount.is_positive():
                    payee = entry.account_id
            if outflow > 0 and payee:
                yield tx.transaction_id, intent_id, outflow, payee

    def _audit(self, event_type: str, payload: dict[str, Any], *, at: datetime):
        """監査ログが接続されていればイベントを追記する（明示時刻で決定論的に）。"""
        if self._audit_log is None:
            return None
        return self._audit_log.append(event_type, payload, timestamp=at)
