"""SmartFPSSource 状态机单元测试

测试覆盖：
- UNINITIALIZED → DISCOVERING → ACTIVE 正常流程
- DISCOVERING → PENDING → ACTIVE 预热流程
- ACTIVE → RECOVERING → ACTIVE 恢复流程
- ACTIVE → PAUSED 长期无帧
- PAUSED → DISCOVERING 重试
- UNSUPPORTED 拉黑逻辑
- Sticky Source 记忆
"""

import time
import pytest

from core.fps_sources import (
    FPSState, FPSResult, SmartFPSState, SmartFPSSource,
    SFBuffFPS, TimeStatsFPS, SFLatencyFPS, GfxInfoFPS,
)


# ─── Mock Source ────────────────────────

class MockSource:
    """可控的 FPS 数据源，按序返回预设的 FPSResult"""
    def __init__(self, results: list[FPSResult]):
        self._results = list(results)
        self._index = 0
        self.call_count = 0

    def read(self) -> FPSResult:
        self.call_count += 1
        if self._index < len(self._results):
            r = self._results[self._index]
            self._index += 1
            return r
        # 默认返回 NO_FRAME
        return FPSResult(FPSState.NO_FRAME, 0.0)


def _make_smart(*sources: tuple[str, MockSource]) -> SmartFPSSource:
    """创建注入了 MockSource 的 SmartFPSSource（跳过 ADB 探测）"""
    return SmartFPSSource._create_for_testing(list(sources))


# ─── Tests ──────────────────────────────

class TestDiscovery:
    """UNINITIALIZED → DISCOVERING → ACTIVE"""

    def test_first_source_ready(self):
        src = MockSource([FPSResult(FPSState.READY, 60.0)])
        sm = _make_smart(("sf_buffer", src))

        # 第一次调用: UNINITIALIZED → DISCOVERING
        assert sm.read_fps() is None
        assert sm._sm_state == SmartFPSState.DISCOVERING

        # 第二次调用: DISCOVERING, 源立即返回 READY → ACTIVE
        fps = sm.read_fps()
        assert fps == 60.0
        assert sm._sm_state == SmartFPSState.ACTIVE
        assert sm._active_name == "sf_buffer"

    def test_skip_unsupported_then_ready(self):
        src1 = MockSource([FPSResult(FPSState.UNSUPPORTED)])
        src2 = MockSource([FPSResult(FPSState.READY, 30.0)])
        sm = _make_smart(("sf_buffer", src1), ("timestats", src2))

        sm.read_fps()  # UNINITIALIZED → DISCOVERING
        fps = sm.read_fps()  # sf_buffer unsupported, timestats ready
        assert fps == 30.0
        assert sm._sm_state == SmartFPSState.ACTIVE
        assert sm._active_name == "timestats"
        assert "sf_buffer" in sm._blacklist

    def test_all_sources_exhausted_to_paused(self):
        src1 = MockSource([FPSResult(FPSState.UNSUPPORTED)])
        src2 = MockSource([FPSResult(FPSState.UNSUPPORTED)])
        sm = _make_smart(("a", src1), ("b", src2))

        sm.read_fps()  # UNINITIALIZED → DISCOVERING
        sm.read_fps()  # a unsupported
        sm.read_fps()  # b unsupported → PAUSED
        assert sm._sm_state == SmartFPSState.PAUSED


class TestPending:
    """DISCOVERING → PENDING → ACTIVE (源需要预热)"""

    def test_warmup_then_ready(self):
        src = MockSource([
            FPSResult(FPSState.WARMUP),   # 探测时返回 WARMUP
            FPSResult(FPSState.READY, 45.0),  # 预热后返回 READY
        ])
        sm = _make_smart(("sf_buffer", src))

        sm.read_fps()  # UNINITIALIZED → DISCOVERING
        sm.read_fps()  # DISCOVERING: WARMUP → PENDING
        assert sm._sm_state == SmartFPSState.PENDING

        fps = sm.read_fps()  # PENDING: READY → ACTIVE
        assert fps == 45.0
        assert sm._sm_state == SmartFPSState.ACTIVE

    def test_pending_unsupported_switches_source(self):
        src1 = MockSource([
            FPSResult(FPSState.WARMUP),
            FPSResult(FPSState.UNSUPPORTED),
        ])
        src2 = MockSource([FPSResult(FPSState.READY, 60.0)])
        sm = _make_smart(("a", src1), ("b", src2))

        sm.read_fps()  # UNINITIALIZED → DISCOVERING
        sm.read_fps()  # a WARMUP → PENDING
        fps = sm.read_fps()  # a UNSUPPORTED → 拉黑, 切到 b
        assert fps == 60.0
        assert sm._sm_state == SmartFPSState.ACTIVE
        assert sm._active_name == "b"
        assert "a" in sm._blacklist


class TestActive:
    """ACTIVE 状态下的正常工作和异常处理"""

    def test_ready_resets_counters(self):
        src = MockSource([
            FPSResult(FPSState.READY, 60.0),
            FPSResult(FPSState.READY, 59.5),
        ])
        sm = _make_smart(("sf_buffer", src))
        sm.read_fps()  # → DISCOVERING
        sm.read_fps()  # → ACTIVE

        sm._consecutive_failures = 5
        fps = sm.read_fps()
        assert fps == 59.5
        assert sm._consecutive_failures == 0

    def test_no_frame_threshold_to_paused(self):
        results = [FPSResult(FPSState.READY, 60.0)]
        # 30 次 NO_FRAME 后应进入 PAUSED
        for _ in range(SmartFPSSource.NO_FRAME_THRESHOLD):
            results.append(FPSResult(FPSState.NO_FRAME, 0.0))

        src = MockSource(results)
        sm = _make_smart(("sf_buffer", src))
        sm.read_fps()  # → DISCOVERING
        sm.read_fps()  # → ACTIVE (READY)

        for i in range(SmartFPSSource.NO_FRAME_THRESHOLD - 1):
            fps = sm.read_fps()
            assert fps == 0.0
            assert sm._sm_state == SmartFPSState.ACTIVE

        # 第 30 次 NO_FRAME → PAUSED
        fps = sm.read_fps()
        assert fps == 0.0
        assert sm._sm_state == SmartFPSState.PAUSED

    def test_transient_fail_threshold_to_recovering(self):
        results = [FPSResult(FPSState.READY, 60.0)]
        for _ in range(SmartFPSSource.FAIL_THRESHOLD):
            results.append(FPSResult(FPSState.TRANSIENT_FAIL))

        src = MockSource(results)
        sm = _make_smart(("sf_buffer", src))
        sm.read_fps()  # → DISCOVERING
        sm.read_fps()  # → ACTIVE

        for _ in range(SmartFPSSource.FAIL_THRESHOLD - 1):
            sm.read_fps()
            assert sm._sm_state == SmartFPSState.ACTIVE

        sm.read_fps()
        assert sm._sm_state == SmartFPSState.RECOVERING

    def test_unsupported_blacklists_and_switches(self):
        src1 = MockSource([
            FPSResult(FPSState.READY, 60.0),
            FPSResult(FPSState.UNSUPPORTED),
        ])
        src2 = MockSource([FPSResult(FPSState.READY, 30.0)])
        sm = _make_smart(("a", src1), ("b", src2))

        sm.read_fps()  # → DISCOVERING
        sm.read_fps()  # → ACTIVE (a)
        fps = sm.read_fps()  # a UNSUPPORTED → 切到 b
        assert fps == 30.0
        assert sm._active_name == "b"
        assert "a" in sm._blacklist


class TestRecovering:
    """ACTIVE → RECOVERING → ACTIVE"""

    def test_recovery_success(self):
        results = [FPSResult(FPSState.READY, 60.0)]
        for _ in range(SmartFPSSource.FAIL_THRESHOLD):
            results.append(FPSResult(FPSState.TRANSIENT_FAIL))
        results.append(FPSResult(FPSState.READY, 55.0))

        src = MockSource(results)
        sm = _make_smart(("sf_buffer", src))
        sm.read_fps()  # → DISCOVERING
        sm.read_fps()  # → ACTIVE
        for _ in range(SmartFPSSource.FAIL_THRESHOLD):
            sm.read_fps()
        assert sm._sm_state == SmartFPSState.RECOVERING

        fps = sm.read_fps()  # 恢复成功
        assert fps == 55.0
        assert sm._sm_state == SmartFPSState.ACTIVE

    def test_recovery_no_frame_still_recovers(self):
        results = [FPSResult(FPSState.READY, 60.0)]
        for _ in range(SmartFPSSource.FAIL_THRESHOLD):
            results.append(FPSResult(FPSState.TRANSIENT_FAIL))
        results.append(FPSResult(FPSState.NO_FRAME, 0.0))

        src = MockSource(results)
        sm = _make_smart(("sf_buffer", src))
        sm.read_fps()
        sm.read_fps()
        for _ in range(SmartFPSSource.FAIL_THRESHOLD):
            sm.read_fps()

        fps = sm.read_fps()  # NO_FRAME 也算恢复
        assert fps == 0.0
        assert sm._sm_state == SmartFPSState.ACTIVE


class TestPausedRecovery:
    """PAUSED → DISCOVERING 重试"""

    def test_paused_retries_after_interval(self):
        results = [FPSResult(FPSState.READY, 60.0)]
        for _ in range(SmartFPSSource.NO_FRAME_THRESHOLD):
            results.append(FPSResult(FPSState.NO_FRAME, 0.0))
        # 暂停后恢复
        results.append(FPSResult(FPSState.READY, 60.0))

        src = MockSource(results)
        sm = _make_smart(("sf_buffer", src))
        sm.read_fps()  # → DISCOVERING
        sm.read_fps()  # → ACTIVE
        for _ in range(SmartFPSSource.NO_FRAME_THRESHOLD):
            sm.read_fps()
        assert sm._sm_state == SmartFPSState.PAUSED

        # 强制跳过重试间隔
        sm._pause_time = time.monotonic() - SmartFPSSource.PAUSED_RETRY_INTERVAL - 1

        fps = sm.read_fps()  # 重试 → ACTIVE
        assert fps == 60.0
        assert sm._sm_state == SmartFPSState.ACTIVE


class TestFPSResult:
    """FPSResult 数据类"""

    def test_ready_with_fps(self):
        r = FPSResult(FPSState.READY, 60.0)
        assert r.state == FPSState.READY
        assert r.fps == 60.0

    def test_no_frame(self):
        r = FPSResult(FPSState.NO_FRAME, 0.0)
        assert r.state == FPSState.NO_FRAME
        assert r.fps == 0.0

    def test_unsupported_default_fps(self):
        r = FPSResult(FPSState.UNSUPPORTED)
        assert r.fps is None


class TestBlacklist:
    """黑名单逻辑"""

    def test_blacklisted_source_skipped(self):
        src1 = MockSource([FPSResult(FPSState.UNSUPPORTED)])
        src2 = MockSource([FPSResult(FPSState.READY, 30.0), FPSResult(FPSState.READY, 30.0)])
        sm = _make_smart(("a", src1), ("b", src2))

        sm.read_fps()  # → DISCOVERING
        sm.read_fps()  # a unsupported → 拉黑
        assert "a" in sm._blacklist
        # 后续探测应跳过 a
        fps = sm.read_fps()
        assert fps == 30.0
        assert sm._active_name == "b"
