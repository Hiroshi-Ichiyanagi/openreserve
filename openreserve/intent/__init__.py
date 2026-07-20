"""
intent — インテント（意図）ベースの自律決済オーソライゼーション + その証明。

「事前に大枠の予算枠（Mandate）を信託し、枠内で自律エージェントが決済を繰り返す」
モデルの **検証コア**。決済実行そのもの（オンチェーン実行等）は範囲外で、ここが
担保するのは「枠を一度も超えていない」ことをオフラインで検証できることである。

- Mandate / IntentSettlementEngine / IntentSettlementResult : 枠と自律決済
- ProofOfMandate / ProofOfMandateGenerator                 : 予算・担保・履歴の証明
"""

from openreserve.intent.mandate import (
    IntentSettlementEngine,
    IntentSettlementResult,
    Mandate,
    MandateError,
)
from openreserve.intent.proofs import ProofOfMandate, ProofOfMandateGenerator

__all__ = [
    "IntentSettlementEngine",
    "IntentSettlementResult",
    "Mandate",
    "MandateError",
    "ProofOfMandate",
    "ProofOfMandateGenerator",
]
