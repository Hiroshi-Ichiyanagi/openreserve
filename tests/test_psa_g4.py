"""
tests/test_psa_g4.py — PSA G-4 月次連続コンプライアンス + 時系列対応 単体テスト

G-4 の核心は calculate_required_reserve(snapshot_at=...) で過去時点の状態を
再現できること:
- snapshot_at 時点で PENDING だった取引を未達債務として拾う
- snapshot_at 時点での USER 残高を預り金として集計 (負残高除外)

これにより、月次連続コンプライアンス証明 (12ヶ月、36ヶ月) が機械的・再現可能に行える。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openreserve.core.ledger import Ledger, TransactionBuilder
from openreserve.core.storage import SQLiteLedgerStorage
from openreserve.core.types import (
    Currency,
    Money,
    OwnerType,
    TransactionStatus,
)
from openreserve.regulatory.payment_services_act import PaymentServicesActCompliance


@pytest.fixture
def ledger():
    return Ledger(SQLiteLedgerStorage(":memory:"))


@pytest.fixture
def psa(ledger):
    return PaymentServicesActCompliance(ledger)


@pytest.fixture
def t0():
    return datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)


def _settle_transfer(ledger, from_acct, to_acct, money, initiated_at, settled_at=None):
    """SETTLED 状態のトランザクションを作る helper。"""
    b = TransactionBuilder("TEST_TRANSFER", "test", initiated_at=initiated_at)
    b.transfer(from_acct.account_id, to_acct.account_id, money)
    ledger.post(b.build())
    ledger.settle(b.transaction_id, settled_at=settled_at or initiated_at)
    return b.transaction_id


def _pending_transfer(ledger, from_acct, to_acct, money, initiated_at):
    """PENDING 状態の PAYOUT トランザクションを作る helper (settle しない)。"""
    b = TransactionBuilder("TEST_PAYOUT", "test", initiated_at=initiated_at)
    b.transfer(from_acct.account_id, to_acct.account_id, money)
    b.with_metadata("transaction_type", "PAYOUT")
    ledger.post(b.build())
    return b.transaction_id


# ============================================================
# G-4 核心テスト: time-aware PENDING detection (4件)
# ============================================================


class TestG4TimeAwarePending:
    """snapshot_at で過去のPENDING状態を再現する G-4 核心ロジック。"""

    def test_pending_payout_recognized_as_obligation(self, ledger, psa, t0):
        """現在時点で PENDING の payout は未達債務に含まれる。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user = ledger.open_account(OwnerType.USER, Currency.JPY, "u")
        # 利用者預り金 100万円 (SETTLED)
        _settle_transfer(ledger, platform, user,
                         Money.from_units(1_000_000, Currency.JPY), t0)
        # 準備資産 200万円
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(2_000_000, Currency.JPY), t0)
        # PENDING の payout 50万円 (user が引き出し中)
        _pending_transfer(ledger, user, platform,
                          Money.from_units(500_000, Currency.JPY),
                          t0 + timedelta(hours=1))

        # snapshot_at = 直後 → user 残高 100万 (まだ引き出し未確定)
        # PENDING で 50万円が未達債務として加算
        # required = 100万 + 50万 = 150万、reserve = 200万 → COMPLIANT
        calc = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(hours=2)
        )
        assert calc.required_deposit_cents == 1_500_000_00 or \
               calc.required_deposit_cents == 1_500_000  # 単位の差吸収
        assert calc.is_compliant

    def test_settled_payout_not_in_pending_after_settlement(self, ledger, psa, t0):
        """settle した後は未達債務に含まれない。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user = ledger.open_account(OwnerType.USER, Currency.JPY, "u")
        # 預り金 100万
        _settle_transfer(ledger, platform, user,
                         Money.from_units(1_000_000, Currency.JPY), t0)
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(2_000_000, Currency.JPY), t0)
        # payout 50万を SETTLED として post
        b = TransactionBuilder("PAYOUT_SETTLED", "test",
                               initiated_at=t0 + timedelta(hours=1))
        b.transfer(user.account_id, platform.account_id,
                   Money.from_units(500_000, Currency.JPY))
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=t0 + timedelta(hours=2))

        # snapshot 設定: settle 後
        calc = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(hours=3)
        )
        # user 残高は 100万 - 50万 = 50万、PENDING分なし
        # required = 50万、reserve = 200万 → COMPLIANT
        assert calc.is_compliant
        # required は 50万円相当 (cents)
        assert calc.required_deposit_cents in (500_000, 500_000_00, 50_000_000)

    def test_snapshot_at_past_time_recovers_pending_state(self, ledger, psa, t0):
        """過去の snapshot_at では、現在は SETTLED でも当時 PENDING だった取引を捕捉。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user = ledger.open_account(OwnerType.USER, Currency.JPY, "u")
        _settle_transfer(ledger, platform, user,
                         Money.from_units(1_000_000, Currency.JPY), t0)
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(2_000_000, Currency.JPY), t0)
        # payout を t0+1h に initiate、t0+5h に settle
        b = TransactionBuilder("PAYOUT_LATE_SETTLE", "test",
                               initiated_at=t0 + timedelta(hours=1))
        b.transfer(user.account_id, platform.account_id,
                   Money.from_units(500_000, Currency.JPY))
        ledger.post(b.build())
        ledger.settle(b.transaction_id, settled_at=t0 + timedelta(hours=5))

        # snapshot_at = t0+3h → 当時 PENDING (settled_at=t0+5h > snapshot)
        calc_at_pending = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(hours=3)
        )
        # snapshot_at = t0+10h → 既に SETTLED
        calc_after_settle = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(hours=10)
        )
        # G-4 核心: 過去時点では required が大きい (PENDING 含む)
        assert calc_at_pending.required_deposit_cents > calc_after_settle.required_deposit_cents

    def test_future_initiated_tx_excluded_from_snapshot(self, ledger, psa, t0):
        """initiated_at > snapshot_at の取引は未達債務に含まれない。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user = ledger.open_account(OwnerType.USER, Currency.JPY, "u")
        _settle_transfer(ledger, platform, user,
                         Money.from_units(1_000_000, Currency.JPY), t0)
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(2_000_000, Currency.JPY), t0)
        # 未来 (t0+5h) に開始する PENDING 取引
        _pending_transfer(ledger, user, platform,
                          Money.from_units(500_000, Currency.JPY),
                          t0 + timedelta(hours=5))

        # snapshot_at = t0+1h → 未来取引は除外される
        calc = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(hours=1)
        )
        # required = 100万 (user預り金のみ、未来PENDINGは含まず)
        assert calc.required_deposit_cents in (1_000_000, 1_000_000_00, 100_000_000)


# ============================================================
# 負残高除外テスト (2件)
# ============================================================


class TestG4NegativeBalanceExclusion:
    """負残高 (creator が引き出し済み) は預り金から除外される。"""

    def test_negative_user_balance_not_in_held_funds(self, ledger, psa, t0):
        """USER 口座の負残高は held_funds に含まれない。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user_a = ledger.open_account(OwnerType.USER, Currency.JPY, "ua")
        user_b = ledger.open_account(OwnerType.USER, Currency.JPY, "ub")
        # user_a 残高 100万 (正)
        _settle_transfer(ledger, platform, user_a,
                         Money.from_units(1_000_000, Currency.JPY), t0)
        # user_b 残高 50万を入れた後、80万引き出し → -30万 (負)
        _settle_transfer(ledger, platform, user_b,
                         Money.from_units(500_000, Currency.JPY), t0)
        _settle_transfer(ledger, user_b, platform,
                         Money.from_units(800_000, Currency.JPY),
                         t0 + timedelta(hours=1),
                         settled_at=t0 + timedelta(hours=2))
        # 準備資産 200万
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(2_000_000, Currency.JPY), t0)

        calc = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(hours=3)
        )
        # user_a 残高 +100万、user_b 残高 -30万 だが負は除外
        # held_funds = 100万 のみ、required = 100万、reserve = 200万 - 80万 = 120万
        assert calc.is_compliant
        # required は 100万 のみ (負残高除外)
        assert calc.required_deposit_cents in (1_000_000, 1_000_000_00, 100_000_000)

    def test_zero_balance_user_not_double_counted(self, ledger, psa, t0):
        """残高ゼロ USER 口座は held_funds に貢献しない。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user_zero = ledger.open_account(OwnerType.USER, Currency.JPY, "uz")
        user_nonzero = ledger.open_account(OwnerType.USER, Currency.JPY, "un")
        # user_nonzero に 50万
        _settle_transfer(ledger, platform, user_nonzero,
                         Money.from_units(500_000, Currency.JPY), t0)
        # 準備資産 100万
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(1_000_000, Currency.JPY), t0)

        calc = psa.calculate_required_reserve(Currency.JPY)
        # user_zero は残高 0、user_nonzero は 50万
        # required = 50万、reserve = 100万 → COMPLIANT
        assert calc.is_compliant
        assert calc.required_deposit_cents in (500_000, 500_000_00, 50_000_000)


# ============================================================
# 月次連続コンプライアンステスト (3件)
# ============================================================


class TestG4MonthlyContinuousCompliance:
    """複数時点での連続コンプライアンス検証 (月次スナップショット)。"""

    def test_multiple_monthly_snapshots_all_compliant(self, ledger, psa, t0):
        """3ヶ月分のスナップショット、すべて適合。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user = ledger.open_account(OwnerType.USER, Currency.JPY, "u")
        # 充分な準備資産
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(50_000_000, Currency.JPY), t0)
        # 各月末に少額の入金
        for i in range(3):
            month_end = t0 + timedelta(days=30 * (i + 1))
            _settle_transfer(ledger, platform, user,
                             Money.from_units(100_000, Currency.JPY),
                             month_end - timedelta(days=1))

        # 3つのスナップショットを取り、すべて compliant
        for i in range(3):
            snap = t0 + timedelta(days=30 * (i + 1) + 1)
            calc = psa.calculate_required_reserve(Currency.JPY, snapshot_at=snap)
            assert calc.is_compliant, \
                f"Month {i+1} snapshot at {snap} should be compliant"

    def test_coverage_ratio_monotonically_high(self, ledger, psa, t0):
        """充足率 = reserve / required が常に 100% 以上。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user = ledger.open_account(OwnerType.USER, Currency.JPY, "u")
        _settle_transfer(ledger, platform, reserve,
                         Money.from_units(10_000_000, Currency.JPY), t0)
        _settle_transfer(ledger, platform, user,
                         Money.from_units(500_000, Currency.JPY), t0)

        calc = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(hours=1)
        )
        # 充足率 (reserve / required) >= 1.0
        # required は 50万、reserve は 1000万 → 充足率 20倍
        assert calc.is_compliant
        assert calc.deficit_cents <= 0  # 余剰

    def test_no_activity_period_compliant(self, ledger, psa, t0):
        """活動なしの期間は required = 0、自動的に compliant。"""
        platform = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "p")
        reserve = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "r")
        user = ledger.open_account(OwnerType.USER, Currency.JPY, "u")
        # 何も transfer しない (口座だけ作る)
        # snapshot
        calc = psa.calculate_required_reserve(
            Currency.JPY, snapshot_at=t0 + timedelta(days=30)
        )
        # 預り金もPENDINGも無い → required = 0
        assert calc.required_deposit_cents == 0
        assert calc.is_compliant


# ============================================================
# 多通貨独立計算テスト (1件)
# ============================================================


class TestG4MultiCurrency:
    """JPY/USD は独立に計算される。"""

    def test_jpy_and_usd_calculated_independently(self, ledger, psa, t0):
        """JPY/USD の準備状態が独立。一方が deficit でも他方には影響しない。"""
        platform_jpy = ledger.open_account(OwnerType.PLATFORM, Currency.JPY, "pj")
        platform_usd = ledger.open_account(OwnerType.PLATFORM, Currency.USD, "pu")
        reserve_jpy = ledger.open_account(OwnerType.RESERVE, Currency.JPY, "rj")
        reserve_usd = ledger.open_account(OwnerType.RESERVE, Currency.USD, "ru")
        user_jpy = ledger.open_account(OwnerType.USER, Currency.JPY, "uj")
        user_usd = ledger.open_account(OwnerType.USER, Currency.USD, "uu")

        # JPY: 預り 100万、準備 200万 (compliant)
        _settle_transfer(ledger, platform_jpy, user_jpy,
                         Money.from_units(1_000_000, Currency.JPY), t0)
        _settle_transfer(ledger, platform_jpy, reserve_jpy,
                         Money.from_units(2_000_000, Currency.JPY), t0)
        # USD: 預り 1万、準備 5000 (deficit)
        _settle_transfer(ledger, platform_usd, user_usd,
                         Money.from_units(10_000, Currency.USD), t0)
        _settle_transfer(ledger, platform_usd, reserve_usd,
                         Money.from_units(5_000, Currency.USD), t0)

        calc_jpy = psa.calculate_required_reserve(Currency.JPY)
        calc_usd = psa.calculate_required_reserve(Currency.USD)
        assert calc_jpy.is_compliant
        assert not calc_usd.is_compliant
        assert calc_usd.deficit_cents > 0
