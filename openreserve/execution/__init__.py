"""
execution — MEV / アビトラージ回収の会計・証明コア（③）。

「利用者には手数料 0%、しかしシステムは自律的に稼ぐ」ゲートウェイの検証コア。
ライブチェーンには一切アクセスしない — 見積り／ルート／入札はすべて明示入力とし、
「正直な回収と公正な分配」を決定論的に証明する。

インポート規律（テストで静的に強制）：このパッケージは openreserve.intent を
一切 import しない。橋渡し（settle_via_auction）は intent 側が execution を import する
一方向のみ。
"""

from openreserve.execution.accounts import ExecutionAccounts
from openreserve.execution.arbitrage import (
    CyclicArbOpportunity,
    PoolEdge,
    PoolGraph,
    find_best_arbitrage_cycle,
    record_arbitrage_detection,
)
from openreserve.execution.auction import (
    AuctionOutcome,
    SolverBid,
    run_auction,
)
from openreserve.execution.proofs import (
    ProofOfBestExecution,
    ProofOfBestExecutionGenerator,
    ProofOfSurplusRecapture,
    ProofOfSurplusRecaptureGenerator,
    SurplusEvent,
)
from openreserve.execution.routing import (
    ExecutionVenue,
    Pool,
    RouteQuote,
    RouteSelection,
    select_best_route,
)
from openreserve.execution.surplus import (
    SplitAllocation,
    SplitPolicyError,
    SurplusSplitPolicy,
    compute_split,
    post_surplus_split,
)

__all__ = [
    # accounts
    "ExecutionAccounts",
    # routing
    "ExecutionVenue",
    "Pool",
    "RouteQuote",
    "RouteSelection",
    "select_best_route",
    # auction
    "SolverBid",
    "AuctionOutcome",
    "run_auction",
    # surplus
    "SurplusSplitPolicy",
    "SplitAllocation",
    "SplitPolicyError",
    "compute_split",
    "post_surplus_split",
    "SurplusEvent",
    # arbitrage
    "PoolEdge",
    "PoolGraph",
    "CyclicArbOpportunity",
    "find_best_arbitrage_cycle",
    "record_arbitrage_detection",
    # proofs
    "ProofOfBestExecution",
    "ProofOfBestExecutionGenerator",
    "ProofOfSurplusRecapture",
    "ProofOfSurplusRecaptureGenerator",
]
