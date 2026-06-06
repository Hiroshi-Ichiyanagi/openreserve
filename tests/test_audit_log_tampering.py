"""transparency/audit_log.py に対する改竄検知強化テスト (Day 10 追加)。

Day 9 で確立した「進化ならぬ深化」リズムの第 2 歩。
既存 test_audit_log.py で検証されているのは単純な改竄検知 (payload 改竄、
event_hash 改竄) のみ。本ファイルでは、より洗練された攻撃シナリオを
ストレステスト形式で検証する。

テスト分類:
- Cat-AUD-MID: 中間ノード改竄検知 (3 件)
- Cat-AUD-RPL: リプレイ攻撃検知 (2 件)
- Cat-AUD-TS:  タイムスタンプ整合性 (2 件)
- Cat-AUD-CHN: チェーン連続性 (2 件)
- Cat-AUD-MUL: 多重改竄検知 (1 件)

合計: 10 件
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from openreserve.transparency.audit_log import GENESIS_HASH, AuditLog


class TestAuditLogTamperingMidNode:
    """中間ノード改竄検知の強化テスト (Cat-AUD-MID)。"""

    def test_aud_mid_01_single_middle_node_payload_tampering(self):
        """Cat-AUD-MID-01: 100 イベント中、中央 1 件の payload 改竄を検知。"""
        log = AuditLog(":memory:")
        for i in range(100):
            log.append(f"event_{i}", {"index": i, "value": i * 10})

        # 中央 (sequence=50) の payload を改竄
        log._conn.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = ?",
            ('{"index": 50, "value": 99999}', 50),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        assert "sequence 50" in err
        assert "Event hash mismatch" in err

    def test_aud_mid_02_multiple_parallel_node_tampering(self):
        """Cat-AUD-MID-02: 複数ノード並列改竄でも最初の改竄が検知される。"""
        log = AuditLog(":memory:")
        for i in range(50):
            log.append(f"event_{i}", {"index": i})

        # 5 箇所同時改竄 (sequence: 10, 20, 30, 40, 45)
        for seq in [10, 20, 30, 40, 45]:
            log._conn.execute(
                "UPDATE audit_events SET payload = ? WHERE sequence = ?",
                ('{"index": ' + str(seq) + ', "tampered": true}', seq),
            )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        # iter_events は sequence 順なので、最初の改竄 (sequence=10) で検知
        assert "sequence 10" in err

    def test_aud_mid_03_boundary_first_and_last_tampering(self):
        """Cat-AUD-MID-03: 境界 (最初と最後) の改竄を検知。"""
        log = AuditLog(":memory:")
        for i in range(20):
            log.append(f"event_{i}", {"index": i})

        # 最初 (sequence=0) と最後 (sequence=19) を改竄
        log._conn.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = ?",
            ('{"tampered": "first"}', 0),
        )
        log._conn.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = ?",
            ('{"tampered": "last"}', 19),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        # 最初に検知されるのは sequence=0 の改竄
        assert "sequence 0" in err


class TestAuditLogTamperingReplay:
    """リプレイ攻撃検知の強化テスト (Cat-AUD-RPL)。"""

    def test_aud_rpl_01_event_hash_swap_blocked_by_unique_constraint(self):
        """Cat-AUD-RPL-01: event_hash 入替えは UNIQUE 制約で先に弾かれる (多層防御)。

        Day 10 発見: event_hash カラムに UNIQUE 制約が設定されており、
        verify_chain() で検知する前に SQL レベルで弾かれる。
        これは意図せぬ多層防御の証跡 (副次的発見 #21 候補)。
        """
        import sqlite3
        log = AuditLog(":memory:")
        ev0 = log.append("e0", {"v": 0})
        ev1 = log.append("e1", {"v": 1})
        ev2 = log.append("e2", {"v": 2})

        # event_hash 入替え試行は UNIQUE 制約で先に弾かれる
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            log._conn.execute(
                "UPDATE audit_events SET event_hash = ? WHERE sequence = ?",
                (ev2.event_hash, 1),
            )
        assert "UNIQUE" in str(exc_info.value)

        # チェーン整合性は保たれている (改竄試行が成功していない)
        is_valid, err = log.verify_chain()
        assert is_valid
        assert err is None

    def test_aud_rpl_02_prev_hash_inconsistency_detected(self):
        """Cat-AUD-RPL-02: prev_hash の偽装を検知 (チェーン破壊)。"""
        log = AuditLog(":memory:")
        for i in range(10):
            log.append(f"e_{i}", {"i": i})

        # sequence=5 の prev_hash を偽の値に変更
        fake_hash = "f" * 64
        log._conn.execute(
            "UPDATE audit_events SET prev_hash = ? WHERE sequence = ?",
            (fake_hash, 5),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        assert "sequence 5" in err
        assert "Hash chain broken" in err


class TestAuditLogTamperingTimestamp:
    """タイムスタンプ整合性の強化テスト (Cat-AUD-TS)。"""

    def test_aud_ts_01_timestamp_forgery_detected(self):
        """Cat-AUD-TS-01: timestamp 偽装は event_hash 不整合として検知。"""
        log = AuditLog(":memory:")
        ts0 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(5):
            log.append(f"e_{i}", {"i": i}, timestamp=ts0 + timedelta(minutes=i))

        # sequence=2 の timestamp を 1 年遡らせる (過去日時偽装)
        forged_ts = (ts0 - timedelta(days=365)).isoformat()
        log._conn.execute(
            "UPDATE audit_events SET timestamp = ? WHERE sequence = ?",
            (forged_ts, 2),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        # timestamp が compute_hash に含まれるので Event hash mismatch として検知
        assert "Event hash mismatch" in err
        assert "sequence 2" in err

    def test_aud_ts_02_timestamp_rollback_detected(self):
        """Cat-AUD-TS-02: timestamp 巻き戻り (微小な変更でも) 検知。"""
        log = AuditLog(":memory:")
        ts0 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            log.append(f"e_{i}", {"i": i}, timestamp=ts0 + timedelta(seconds=i))

        # sequence=7 の timestamp を 1 秒遡らせる (微小巻き戻り)
        rolled_back = (ts0 + timedelta(seconds=6)).isoformat()
        log._conn.execute(
            "UPDATE audit_events SET timestamp = ? WHERE sequence = ?",
            (rolled_back, 7),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        assert "sequence 7" in err


class TestAuditLogTamperingChain:
    """チェーン連続性の強化テスト (Cat-AUD-CHN)。"""

    def test_aud_chn_01_event_deletion_detected(self):
        """Cat-AUD-CHN-01: 中間イベント削除を sequence gap として検知。"""
        log = AuditLog(":memory:")
        for i in range(10):
            log.append(f"e_{i}", {"i": i})

        # sequence=5 を削除
        log._conn.execute(
            "DELETE FROM audit_events WHERE sequence = ?",
            (5,),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        # iter_events は残った 9 件を順に処理、sequence=5 の位置で gap を検知
        assert "Sequence gap" in err

    def test_aud_chn_02_event_type_tampering_detected(self):
        """Cat-AUD-CHN-02: event_type の改竄を event_hash 不整合として検知。"""
        log = AuditLog(":memory:")
        for i in range(15):
            log.append(f"sweep_executed", {"i": i})

        # sequence=8 の event_type を改竄 (例: sweep_executed → sweep_failed)
        log._conn.execute(
            "UPDATE audit_events SET event_type = ? WHERE sequence = ?",
            ("sweep_failed", 8),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        # event_type が compute_hash に含まれるので Event hash mismatch として検知
        assert "Event hash mismatch" in err
        assert "sequence 8" in err


class TestAuditLogTamperingMultiple:
    """多重改竄検知の強化テスト (Cat-AUD-MUL)。"""

    def test_aud_mul_01_combined_attack_all_detected(self):
        """Cat-AUD-MUL-01: 複合攻撃 (payload + timestamp + event_hash) が検知される。"""
        log = AuditLog(":memory:")
        ts0 = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(30):
            log.append(f"e_{i}", {"i": i, "value": i * 100}, timestamp=ts0 + timedelta(minutes=i))

        # 3 種類の改竄を異なる sequence で実施
        # sequence=5: payload 改竄
        log._conn.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = ?",
            ('{"i": 5, "value": 555555}', 5),
        )
        # sequence=15: timestamp 改竄
        forged_ts = (ts0 + timedelta(days=999)).isoformat()
        log._conn.execute(
            "UPDATE audit_events SET timestamp = ? WHERE sequence = ?",
            (forged_ts, 15),
        )
        # sequence=25: event_hash 直接改竄
        log._conn.execute(
            "UPDATE audit_events SET event_hash = ? WHERE sequence = ?",
            ("a" * 64, 25),
        )

        is_valid, err = log.verify_chain()
        assert not is_valid
        assert err is not None
        # 最初に検知されるのは sequence=5 (最も若い sequence)
        assert "sequence 5" in err
