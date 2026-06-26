"""FPS 数据源 — SurfaceFlinger / GfxInfo / TimeStats / SmartFPS

架构：状态机驱动的 FPS 采集
- FPSResult 统一返回类型（FPSState + fps）
- SmartFPSState 6 态状态机
- Source 层统一 read() -> FPSResult

优先级:
1. TimeStats
2. SurfaceFlinger Latency
3. GfxInfo
4. SurfaceFlinger Buffer Frame Counter
"""

import re
import time
import logging
from enum import Enum, auto
from dataclasses import dataclass


# ─── FPS 采集常量 ────────────────────────

MAX_FRAME_DELTA = 300        # buffer 帧数差上限（超过视为 surface 重建）
MAX_FPS = 240                # FPS 上限裁剪
SFBUFF_MIN_ELAPSED = 0.5     # SurfaceFlinger Buffer 最小采样间隔（秒）
TIMESTATS_MIN_ELAPSED = 0.3  # TimeStats / GfxInfo 最小采样间隔（秒）


# ═══════════════════════════════════════════
# 基础类型
# ═══════════════════════════════════════════

class FPSState(Enum):
    """Source 层 FPS 读取状态

    设计原则：
    - READY / NO_FRAME 描述的是 Target 的状态（有帧 / 没帧）
    - TARGET_INVALID 描述的是 Target 生命周期变化（原目标消失，需重新发现）
    - TRANSIENT_FAIL / UNSUPPORTED 描述的是 Source 自身状态（临时故障 / 永久不可用）
    """
    READY = auto()           # Target 正常出帧
    NO_FRAME = auto()        # Target 还在，只是没出帧
    WARMUP = auto()          # 预热中（再等等）
    TRANSIENT_FAIL = auto()  # Source 临时失败（稍后重试）
    UNSUPPORTED = auto()     # Source 永久不可用（拉黑）
    TARGET_INVALID = auto()  # Target 已失效（原目标消失，需重新发现）


@dataclass
class FPSResult:
    """Source 统一返回类型"""
    state: FPSState
    fps: float | None = None


class SmartFPSState(Enum):
    """SmartFPS 状态机状态"""
    UNINITIALIZED = auto()  # 尚未开始探测
    DISCOVERING = auto()    # 寻找可用源
    PENDING = auto()        # 等待高优先级源预热
    ACTIVE = auto()         # 稳定输出
    RECOVERING = auto()     # 优先恢复当前源
    PAUSED = auto()         # 息屏/断连


# ═══════════════════════════════════════════
# SurfaceFlinger Buffer Frame Counter
# 优先级 4（保底）
# ═══════════════════════════════════════════

class SFBuffFPS:
    """通过 dumpsys SurfaceFlinger 的 buffer frame 计数

    注意：这是全局 FPS 估计器，并非目标应用 FPS。
    统计所有 state=ACQUIRED surface 的最大 frame 值，
    可能包含系统 UI surface（状态栏、导航栏等）。
    """
    def __init__(self, adb):
        self.adb = adb
        self._prev_buffer_frames = None
        self._prev_buffer_time = None

    def read(self) -> FPSResult:
        out, rc = self.adb.run_shell_retry("dumpsys SurfaceFlinger", timeout=10, retries=2)
        if rc != 0 or not out:
            return FPSResult(FPSState.TRANSIENT_FAIL)

        max_frame = 0
        for line in out.split("\n"):
            if "state=ACQUIRED" in line:
                match = re.search(r"frame=(\d+)", line)
                if match:
                    f = int(match.group(1))
                    if f > max_frame:
                        max_frame = f

        now = time.monotonic()
        if max_frame == 0:
            return FPSResult(FPSState.TRANSIENT_FAIL)

        if self._prev_buffer_frames is None:
            self._prev_buffer_frames = max_frame
            self._prev_buffer_time = now
            return FPSResult(FPSState.WARMUP)

        elapsed = now - self._prev_buffer_time
        if elapsed < SFBUFF_MIN_ELAPSED:
            return FPSResult(FPSState.WARMUP)

        delta = max_frame - self._prev_buffer_frames
        self._prev_buffer_frames = max_frame
        self._prev_buffer_time = now

        if delta < 0 or delta > MAX_FRAME_DELTA:
            return FPSResult(FPSState.TRANSIENT_FAIL)  # surface 重建/切换
        if delta == 0:
            return FPSResult(FPSState.NO_FRAME, 0.0)

        fps = delta / elapsed
        return FPSResult(FPSState.READY, round(min(max(fps, 0), MAX_FPS), 1))


# ═══════════════════════════════════════════
# TimeStats FPS
# 优先级 1（最准确）
# ═══════════════════════════════════════════

class TimeStatsFPS:
    """通过 dumpsys SurfaceFlinger --timestats 获取游戏帧率"""
    def __init__(self, adb, package=None):
        self.adb = adb
        self.package = package
        self._enabled = False
        self._prev_entries: dict[str, int] = {}
        self._prev_global_frames: int | None = None
        self._prev_time: float | None = None
        self._target_layer: str | None = None  # 当前锁定的 layer 名称

    def _enable(self):
        self.adb.run_shell("dumpsys SurfaceFlinger --timestats -enable", timeout=3)
        self._enabled = True

    def _parse_output(self, out):
        """解析 timestats 输出，返回 (entries, global_frames)

        entries: [(layer_name, total_frames, avg_fps), ...] 按 totalFrames 降序
        global_frames: Legacy 全局 totalFrames（系统级帧计数器，实时更新）
        """
        pkg = self.package or ""
        entries = []
        global_frames = None
        current = {}
        seen_layer = False

        def _flush():
            if not seen_layer:
                return
            name = current.get("name", "")
            frames = current.get("frames")
            if frames is not None and "SurfaceView" in name:
                if (not pkg) or (pkg in name):
                    entries.append((name, frames, current.get("fps")))

        for line in out.split("\n"):
            line = line.strip()
            if "layerName" in line:
                _flush()
                current = {"name": line.split("=", 1)[1].strip() if "=" in line else ""}
                seen_layer = True
            elif line.startswith("totalFrames"):
                match = re.search(r"(\d+)", line)
                if match:
                    val = int(match.group(1))
                    current["frames"] = val
                    if not seen_layer:
                        global_frames = val

        _flush()

        entries.sort(key=lambda e: e[1], reverse=True)
        return entries, global_frames

    def read(self) -> FPSResult:
        if not self._enabled:
            self._enable()
            return FPSResult(FPSState.WARMUP)

        out, rc = self.adb.run_shell_retry(
            "dumpsys SurfaceFlinger --timestats -dump", timeout=8, retries=1
        )
        if rc != 0 or not out:
            return FPSResult(FPSState.TRANSIENT_FAIL)

        entries, global_frames = self._parse_output(out)

        if not entries and global_frames is None:
            return FPSResult(FPSState.TRANSIENT_FAIL)

        now = time.monotonic()

        # 首次读取：缓存
        if self._prev_global_frames is None:
            self._prev_entries = {name: frames for name, frames, _ in entries}
            self._prev_global_frames = global_frames
            self._prev_time = now
            return FPSResult(FPSState.WARMUP)

        elapsed = now - self._prev_time
        if elapsed < TIMESTATS_MIN_ELAPSED:
            return FPSResult(FPSState.WARMUP)

        # 检查 target layer 是否仍然存在
        entry_names = {name for name, _, _ in entries}
        if self._target_layer and self._target_layer not in entry_names and entries:
            self._target_layer = None
            return FPSResult(FPSState.TARGET_INVALID)

        # per-layer delta（找活跃 layer）
        best_fps = 0.0
        best_name = None
        for name, frames, _ in entries:
            prev = self._prev_entries.get(name)
            if prev is not None:
                delta = frames - prev
                if delta > 0:
                    fps = delta / elapsed
                    if fps > best_fps:
                        best_fps = fps
                        best_name = name

        # 回退：全局帧计数器 delta
        if best_fps == 0 and global_frames is not None and self._prev_global_frames is not None:
            delta = global_frames - self._prev_global_frames
            if delta > 0:
                best_fps = delta / elapsed

        # 更新缓存
        self._prev_entries = {name: frames for name, frames, _ in entries}
        self._prev_global_frames = global_frames
        self._prev_time = now

        if best_fps > 0:
            if best_name:
                self._target_layer = best_name
            return FPSResult(FPSState.READY, round(min(best_fps, MAX_FPS), 1))
        return FPSResult(FPSState.NO_FRAME, 0.0)

    def cleanup(self):
        if self._enabled:
            self.adb.run_shell(
                "dumpsys SurfaceFlinger --timestats -disable", timeout=3
            )


# ═══════════════════════════════════════════
# SurfaceFlinger Latency FPS
# 优先级 2（也很准确）
# ═══════════════════════════════════════════

class SFLatencyFPS:
    """通过 dumpsys SurfaceFlinger --latency 获取帧时间戳"""
    def __init__(self, adb, package=None):
        self.adb = adb
        self.package = package
        self._cached_window = None
        self._consecutive_failures = 0

    def read(self) -> FPSResult:
        window = self._find_surface_view()
        if not window:
            # 之前有缓存窗口但现在找不到 → 目标失效
            if self._cached_window:
                self._cached_window = None
                return FPSResult(FPSState.TARGET_INVALID)
            return FPSResult(FPSState.WARMUP)
        safe_win = window.replace("'", "'\\''")
        out, rc = self.adb.run_shell_retry(
            f"dumpsys SurfaceFlinger --latency '{safe_win}'", timeout=8, retries=1
        )
        if rc != 0:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 5:
                self._cached_window = None  # 重新寻址
            return FPSResult(FPSState.TRANSIENT_FAIL)

        result = self._parse_latency(out)
        if result is None:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 5:
                self._cached_window = None  # 重新寻址
            return FPSResult(FPSState.TRANSIENT_FAIL)
        else:
            self._consecutive_failures = 0
            return FPSResult(FPSState.READY, result)

    def _parse_latency(self, out):
        if not out:
            return None
        timestamps = []
        for i, line in enumerate(out.strip().split("\n")):
            if i == 0:
                continue
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    t = int(parts[1])
                    if 0 < t < 9223372036854775807:
                        timestamps.append(t)
                except (ValueError, IndexError):
                    continue
        if len(timestamps) < 2:
            return None
        diffs = [(timestamps[i] - timestamps[i - 1]) / 1e9
                 for i in range(1, len(timestamps))
                 if 0.001 < (timestamps[i] - timestamps[i - 1]) / 1e9 < 2.0]
        if not diffs:
            return None
        # len(diffs) = 有效帧间隔数, sum(diffs) = 总经过时间(秒)
        return round(min(len(diffs) / sum(diffs), MAX_FPS), 1)

    def _find_surface_view(self):
        if self._cached_window:
            return self._cached_window
        out, rc = self.adb.run_shell("dumpsys SurfaceFlinger --list")
        if rc != 0 or not out:
            return None
        pkg = self.package or ""

        candidates = []
        seen = set()  # 去重用，O(1) 查找
        for line in out.split("\n"):
            line = line.strip()
            if not line or "Background for" in line:
                continue

            has_blast = "(BLAST)" in line

            sv_match = re.search(
                r"(SurfaceView\[[^\]]+\](?:\(BLAST\))?(?:#\d+)?)", line
            )
            if sv_match:
                name = sv_match.group(1)
                if pkg and pkg in line:
                    candidates.append((1 if has_blast else 2, name))
                else:
                    candidates.append((3 if has_blast else 4, name))
                seen.add(name)
                continue

            if pkg and pkg in line:
                win_match = re.match(r"([^\s]+)", line)
                if win_match:
                    name = win_match.group(1)
                    candidates.append((5, name))
                    seen.add(name)
                continue

            if has_blast:
                win_match = re.match(r"([^\s]+)", line)
                if win_match:
                    name = win_match.group(1)
                    if name not in seen:
                        candidates.append((6, name))
                        seen.add(name)

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        result = candidates[0][1]
        self._cached_window = result
        return result


# ═══════════════════════════════════════════
# GfxInfo FPS
# 优先级 3（有数据时为真正 App FPS）
# ═══════════════════════════════════════════

class GfxInfoFPS:
    """通过 dumpsys gfxinfo 的帧计数差值计算 FPS"""
    def __init__(self, adb, package):
        self.adb = adb
        self.package = package
        self._prev_frames = None
        self._prev_time = None

    def read(self) -> FPSResult:
        if not self.package:
            return FPSResult(FPSState.UNSUPPORTED)

        out, rc = self.adb.run_shell_retry(
            f"dumpsys gfxinfo {self.package}", timeout=8, retries=1
        )
        if rc != 0 or not out:
            return FPSResult(FPSState.TRANSIENT_FAIL)

        total_frames = None
        for line in out.split("\n"):
            if "Total frames rendered:" in line:
                match = re.search(r"(\d+)", line)
                if match:
                    total_frames = int(match.group(1))
                    break

        if total_frames is None:
            return FPSResult(FPSState.TRANSIENT_FAIL)

        now = time.monotonic()
        if self._prev_frames is None:
            self._prev_frames = total_frames
            self._prev_time = now
            return FPSResult(FPSState.WARMUP)

        elapsed = now - self._prev_time
        if elapsed < TIMESTATS_MIN_ELAPSED:
            return FPSResult(FPSState.WARMUP)

        delta = total_frames - self._prev_frames
        self._prev_frames = total_frames
        self._prev_time = now

        if delta <= 0:
            return FPSResult(FPSState.NO_FRAME, 0.0)

        fps = delta / elapsed
        return FPSResult(FPSState.READY, round(min(max(fps, 0), MAX_FPS), 1))


# ═══════════════════════════════════════════
# Smart FPS Source — 状态机驱动
# ═══════════════════════════════════════════

class SmartFPSSource:
    """自动探测并选择最佳 FPS 数据源（状态机驱动）

    State Machine:

        UNINITIALIZED
            ↓
        DISCOVERING
            ├─ READY ──────→ ACTIVE
            ├─ WARMUP ────→ PENDING
            └─ 全失败 ───→ PAUSED

        PENDING
            ├─ READY ──────→ ACTIVE
            ├─ WARMUP ────→ PENDING (继续等)
            ├─ NO_FRAME ──→ PENDING (继续等)
            ├─ UNSUPPORTED → DISCOVERING
            ├─ timeout ───→ DISCOVERING
            └─ TRANSIENT_FAIL → PENDING (继续等)

        ACTIVE
            ├─ READY ──────────────→ ACTIVE
            │    (reset fail/no_data)
            ├─ NO_FRAME ×30 ──────→ PAUSED
            │    (长期静默：游戏退出/息屏)
            ├─ TRANSIENT_FAIL ×10 → RECOVERING
            │    (源暂时异常，尝试恢复)
            └─ UNSUPPORTED ───────→ DISCOVERING
                 (立即放弃该源)

        RECOVERING
            ├─ READY ──────→ ACTIVE
            ├─ NO_FRAME ──→ ACTIVE
            ├─ UNSUPPORTED → DISCOVERING
            ├─ timeout(2轮)→ DISCOVERING
            └─ 全失败 ───→ PAUSED

        PAUSED
            ├─ pause_source READY/NO_FRAME → ACTIVE (优先恢复)
            └─ else ────→ DISCOVERING (重试)

    优先级顺序:
    1. TimeStatsFPS (最准确)
    2. SFLatencyFPS (也很准确)
    3. GfxInfoFPS (有数据时为真正 App FPS)
    4. SFBuffFPS (最后保底)
    """
    # 配置常量
    FAIL_THRESHOLD = 10            # ACTIVE 中连续 TRANSIENT_FAIL 多少次 → RECOVERING
    NO_FRAME_THRESHOLD = 30        # ACTIVE 中连续 NO_FRAME 多少次 → PAUSED
    RECOVERING_TIMEOUT = 3.0       # RECOVERING 阶段超时秒数
    RECOVERING_MAX_ROUNDS = 2      # RECOVERING 最多重试几轮
    PAUSED_RETRY_INTERVAL = 3.0    # PAUSED 状态重试间隔

    # Per-source PENDING 超时（type-based，重构安全）
    PENDING_TIMEOUT: dict[type, float] = {
        TimeStatsFPS: 32.0,
        SFLatencyFPS: 1.2,
        GfxInfoFPS: 1.5,
        SFBuffFPS: 1.0,
    }
    PENDING_TIMEOUT_DEFAULT = 2.0

    def __init__(self, adb, package=None):
        self.adb = adb
        self.package = package

        # Source 列表（按优先级排序）
        self._sources: list[tuple[str, object]] = []
        self._sources.append(("timestats", TimeStatsFPS(adb, package)))
        self._sources.append(("sf_latency", SFLatencyFPS(adb, package)))
        if package:
            self._sources.append(("gfxinfo", GfxInfoFPS(adb, package)))
        self._sources.append(("sf_buffer", SFBuffFPS(adb)))

        # 状态机
        self._sm_state = SmartFPSState.UNINITIALIZED
        self._active_source = None
        self._active_name = None
        self._pending_source = None
        self._pending_name = None
        self._pending_since = 0.0
        self._recovering_source = None
        self._recovering_name = None
        self._recovering_since = 0.0
        self._recovering_rounds = 0
        self._consecutive_failures = 0  # ACTIVE 中连续 TRANSIENT_FAIL 计数
        self._no_data_count = 0         # ACTIVE 中连续 NO_FRAME 计数
        self._pause_source = None       # PAUSED 前的源（优先恢复）
        self._pause_name = None
        self._blacklist: set[str] = set()
        self._discover_index = 0  # DISCOVERING 阶段当前探测的源索引
        self._pause_time = 0.0
        # Sticky Source：设备自适应，记住上次稳定工作的源
        self._sticky_source_name: str | None = None
        self._sticky_tried = False  # 本轮 DISCOVERING 是否已尝试过 sticky

    @classmethod
    def _create_for_testing(cls, sources: list[tuple[str, object]]):
        """测试工厂：注入预构建的 source 列表，跳过 ADB 探测"""
        inst = cls.__new__(cls)
        inst.adb = None
        inst.package = "com.test"
        inst._sources = list(sources)
        inst._sm_state = SmartFPSState.UNINITIALIZED
        inst._active_source = None
        inst._active_name = None
        inst._pending_source = None
        inst._pending_name = None
        inst._pending_since = 0.0
        inst._recovering_source = None
        inst._recovering_name = None
        inst._recovering_since = 0.0
        inst._recovering_rounds = 0
        inst._consecutive_failures = 0
        inst._no_data_count = 0
        inst._pause_source = None
        inst._pause_name = None
        inst._blacklist = set()
        inst._discover_index = 0
        inst._pause_time = 0.0
        inst._sticky_source_name = None
        inst._sticky_tried = False
        return inst

    # ─── 状态转换 ───

    def _set_state(self, new_state: SmartFPSState, reason: str = "") -> None:
        old = self._sm_state
        if old != new_state:
            if reason:
                logging.info("SmartFPS: %s -> %s (%s)", old.name, new_state.name, reason)
            else:
                logging.info("SmartFPS: %s -> %s", old.name, new_state.name)
        self._sm_state = new_state

    # ─── 公开接口：保持 float | None 兼容 ───

    @property
    def active_source_name(self) -> str | None:
        """当前激活的 FPS 数据源名称（sf_buffer / timestats / sf_latency / gfxinfo）"""
        return self._active_name

    def read_fps(self) -> float | None:
        """对外接口，返回 float（正常）或 None（无数据），内部驱动状态机"""
        result = self._drive_state_machine()
        if result is not None and result > 0:
            return result
        if result == 0.0:
            return result
        return None

    # ─── 状态机驱动 ───

    def _drive_state_machine(self) -> float | None:
        match self._sm_state:
            case SmartFPSState.UNINITIALIZED:
                return self._handle_uninitialized()
            case SmartFPSState.DISCOVERING:
                return self._handle_discovering()
            case SmartFPSState.PENDING:
                return self._handle_pending()
            case SmartFPSState.ACTIVE:
                return self._handle_active()
            case SmartFPSState.RECOVERING:
                return self._handle_recovering()
            case SmartFPSState.PAUSED:
                return self._handle_paused()
        return None

    # ─── UNINITIALIZED ───

    def _handle_uninitialized(self) -> float | None:
        self._set_state(SmartFPSState.DISCOVERING, "startup")
        self._discover_index = 0
        self._sticky_tried = False
        return None

    # ─── DISCOVERING ───

    def _handle_discovering(self) -> float | None:
        # Sticky Source：优先尝试上次稳定工作的源
        if not self._sticky_tried and self._sticky_source_name:
            self._sticky_tried = True
            for name, src in self._sources:
                if name == self._sticky_source_name and name not in self._blacklist:
                    result = src.read()
                    logging.debug("SmartFPS: sticky try %s -> %s", name, result.state.name)
                    if result.state == FPSState.READY:
                        self._active_source = src
                        self._active_name = name
                        self._consecutive_failures = 0
                        self._set_state(SmartFPSState.ACTIVE,
                                        f"{name} sticky ready")
                        logging.info("FPS 数据源粘性恢复: %s", name)
                        return result.fps
                    elif result.state == FPSState.WARMUP:
                        # 粘性源预热中，进入 PENDING 等它
                        self._pending_source = src
                        self._pending_name = name
                        self._pending_since = time.monotonic()
                        self._set_state(SmartFPSState.PENDING,
                                        f"{name} sticky warmup")
                        return None
                    # NO_FRAME / TRANSIENT_FAIL / UNSUPPORTED → 继续正常流程
                    break

        while self._discover_index < len(self._sources):
            name, src = self._sources[self._discover_index]

            if name in self._blacklist:
                self._discover_index += 1
                continue

            result = src.read()

            if result.state == FPSState.READY:
                # 立即锁定
                self._active_source = src
                self._active_name = name
                self._consecutive_failures = 0
                self._set_state(SmartFPSState.ACTIVE, f"{name} ready")
                logging.info("FPS 数据源已锁定: %s", name)
                return result.fps

            elif result.state == FPSState.WARMUP:
                # 进入 PENDING，等这个源预热
                self._pending_source = src
                self._pending_name = name
                self._pending_since = time.monotonic()
                self._set_state(SmartFPSState.PENDING, f"{name} warmup")
                return None

            elif result.state == FPSState.NO_FRAME:
                # 源正常但无帧（非游戏场景），跳过继续探测
                self._discover_index += 1
                continue

            elif result.state == FPSState.UNSUPPORTED:
                self._blacklist.add(name)
                logging.info("FPS 数据源 %s 不支持，拉黑", name)
                self._discover_index += 1
                continue

            elif result.state == FPSState.TRANSIENT_FAIL:
                self._discover_index += 1
                continue

        # 所有源都试过了
        self._set_state(SmartFPSState.PAUSED, "all sources exhausted in discovery")
        self._pause_time = time.monotonic()
        return None

    # ─── PENDING ───

    def _get_pending_timeout(self) -> float:
        """获取当前 pending 源的超时时间"""
        if self._pending_source is None:
            return self.PENDING_TIMEOUT_DEFAULT
        return self.PENDING_TIMEOUT.get(
            type(self._pending_source), self.PENDING_TIMEOUT_DEFAULT
        )

    def _handle_pending(self) -> float | None:
        elapsed = time.monotonic() - self._pending_since
        timeout = self._get_pending_timeout()
        if elapsed > timeout:
            # 超时，继续探测下一个源
            logging.info("FPS 数据源 %s 预热超时(%.1fs)，跳过", self._pending_name, timeout)
            self._set_state(SmartFPSState.DISCOVERING,
                            f"{self._pending_name} pending timeout")
            self._discover_index += 1
            self._pending_source = None
            self._pending_name = None
            return self._handle_discovering()

        result = self._pending_source.read()

        if result.state == FPSState.READY:
            self._active_source = self._pending_source
            self._active_name = self._pending_name
            self._consecutive_failures = 0
            self._pending_source = None
            self._pending_name = None
            self._set_state(SmartFPSState.ACTIVE, f"{self._active_name} ready")
            logging.info("FPS 数据源已锁定: %s", self._active_name)
            return result.fps

        elif result.state == FPSState.NO_FRAME:
            # 源正常但当前无新帧，与 WARMUP 语义一致，继续等待。
            # 是否放弃由 PENDING 超时机制统一决定。
            return None

        elif result.state == FPSState.TARGET_INVALID:
            logging.info("FPS 数据源 %s 目标失效，跳过", self._pending_name)
            self._set_state(SmartFPSState.DISCOVERING,
                            f"{self._pending_name} target invalid")
            self._pending_source = None
            self._pending_name = None
            self._discover_index += 1
            return self._handle_discovering()

        elif result.state == FPSState.UNSUPPORTED:
            self._blacklist.add(self._pending_name)
            logging.info("FPS 数据源 %s 不支持，拉黑", self._pending_name)
            self._set_state(SmartFPSState.DISCOVERING,
                            f"{self._pending_name} unsupported")
            self._pending_source = None
            self._pending_name = None
            self._discover_index += 1
            return self._handle_discovering()

        # WARMUP 或 TRANSIENT_FAIL：继续等
        return None

    # ─── ACTIVE ───

    def _handle_active(self) -> float | None:
        result = self._active_source.read()

        if result.state == FPSState.READY:
            # 正常工作，重置所有计数，记录粘性源
            self._consecutive_failures = 0
            self._no_data_count = 0
            self._sticky_source_name = self._active_name
            logging.debug("SmartFPS: %s -> %s (sticky)", self._active_name, result.fps)
            return result.fps

        elif result.state == FPSState.NO_FRAME:
            # NO_FRAME semantics:
            # Short periods (< NO_FRAME_THRESHOLD) are propagated as 0.0 FPS.
            # This preserves legitimate 0 FPS situations such as pause menus,
            # loading screens, and static content.
            # Sustained NO_FRAME periods are treated as inactivity
            # (screen off / application exit), transitioning to PAUSED
            # and suppressing further statistics.
            # AOD detection is intentionally out of scope;
            # a future DisplayStateMonitor should filter display state
            # before SmartFPS consumption.
            self._consecutive_failures = 0
            self._no_data_count += 1
            logging.debug("SmartFPS: %s -> 0 (no_data #%d)",
                          self._active_name, self._no_data_count)
            if self._no_data_count >= self.NO_FRAME_THRESHOLD:
                # 长期静默，进入 PAUSED
                self._pause_source = self._active_source
                self._pause_name = self._active_name
                self._consecutive_failures = 0
                self._no_data_count = 0
                self._set_state(SmartFPSState.PAUSED,
                                f"NO_FRAME x{self.NO_FRAME_THRESHOLD}")
                self._pause_time = time.monotonic()
                logging.info("FPS 数据源 %s 长时间无新帧，暂停", self._active_name)
            return result.fps

        elif result.state == FPSState.TRANSIENT_FAIL:
            # 源暂时不可用（ADB 超时等）
            self._no_data_count = 0
            self._consecutive_failures += 1
            logging.debug("SmartFPS: %s -> TRANSIENT_FAIL (#%d)",
                          self._active_name, self._consecutive_failures)
            if self._consecutive_failures >= self.FAIL_THRESHOLD:
                self._recovering_source = self._active_source
                self._recovering_name = self._active_name
                self._recovering_since = time.monotonic()
                self._recovering_rounds = 0
                self._consecutive_failures = 0
                self._no_data_count = 0
                self._set_state(SmartFPSState.RECOVERING,
                                f"{self._active_name} {self.FAIL_THRESHOLD} transient failures")
                logging.info("FPS 数据源 %s 连续失败，进入恢复", self._active_name)
            return None

        elif result.state == FPSState.TARGET_INVALID:
            # 目标已失效（layer 消失），重新发现，不拉黑
            logging.info("FPS 数据源 %s 目标失效，重新发现", self._active_name)
            self._consecutive_failures = 0
            self._no_data_count = 0
            return self._switch_source()

        elif result.state == FPSState.UNSUPPORTED:
            # 源明确不可用，立即切源并拉黑
            if self._active_name == self._sticky_source_name:
                self._sticky_source_name = None  # 粘性源不可用，清除
            self._blacklist.add(self._active_name)
            self._consecutive_failures = 0
            self._no_data_count = 0
            logging.info("FPS 数据源 %s 变为不支持，切换", self._active_name)
            return self._switch_source()

        elif result.state == FPSState.WARMUP:
            # ACTIVE 中不应出现 WARMUP，视为临时失败
            self._consecutive_failures += 1
            return None

        return None

    # ─── RECOVERING ───

    def _handle_recovering(self) -> float | None:
        elapsed = time.monotonic() - self._recovering_since

        if elapsed > self.RECOVERING_TIMEOUT:
            self._recovering_rounds += 1
            if self._recovering_rounds >= self.RECOVERING_MAX_ROUNDS:
                # 恢复失败，切源
                logging.info("FPS 数据源 %s 恢复失败，切源", self._recovering_name)
                self._recovering_source = None
                self._recovering_name = None
                return self._switch_source()
            # 还有重试轮次
            self._recovering_since = time.monotonic()

        result = self._recovering_source.read()

        if result.state == FPSState.READY:
            # 恢复成功
            self._active_source = self._recovering_source
            self._active_name = self._recovering_name
            self._consecutive_failures = 0
            self._recovering_source = None
            self._recovering_name = None
            self._set_state(SmartFPSState.ACTIVE,
                            f"{self._active_name} recovered")
            logging.info("FPS 数据源 %s 恢复成功", self._active_name)
            return result.fps

        elif result.state == FPSState.NO_FRAME:
            # 源恢复了但无帧，也算恢复成功
            self._active_source = self._recovering_source
            self._active_name = self._recovering_name
            self._consecutive_failures = 0
            self._recovering_source = None
            self._recovering_name = None
            self._set_state(SmartFPSState.ACTIVE,
                            f"{self._active_name} recovered (no frame)")
            logging.info("FPS 数据源 %s 恢复成功（无新帧）", self._active_name)
            return result.fps

        # WARMUP / TRANSIENT_FAIL / UNSUPPORTED / TARGET_INVALID：继续等或放弃
        if result.state == FPSState.TARGET_INVALID:
            logging.info("FPS 数据源 %s 目标失效，放弃恢复", self._recovering_name)
            self._recovering_source = None
            self._recovering_name = None
            return self._switch_source()

        if result.state == FPSState.UNSUPPORTED:
            self._blacklist.add(self._recovering_name)
            logging.info("FPS 数据源 %s 变为不支持，切源", self._recovering_name)
            self._recovering_source = None
            self._recovering_name = None
            return self._switch_source()

        return None

    # ─── PAUSED ───

    def _handle_paused(self) -> float | None:
        now = time.monotonic()
        if now - self._pause_time < self.PAUSED_RETRY_INTERVAL:
            return None

        # 清零计数器
        self._consecutive_failures = 0
        self._no_data_count = 0

        # 优先尝试暂停前的源
        if self._pause_source:
            result = self._pause_source.read()
            if result.state == FPSState.READY:
                self._active_source = self._pause_source
                self._active_name = self._pause_name
                self._pause_source = None
                self._pause_name = None
                self._set_state(SmartFPSState.ACTIVE,
                                f"{self._active_name} recovered from pause")
                logging.info("FPS 数据源 %s 从暂停恢复", self._active_name)
                return result.fps
            # 源仍不可用或无帧，走 DISCOVERING
            self._pause_source = None
            self._pause_name = None

        # 重新进入 DISCOVERING（重置 sticky 尝试标记）
        self._discover_index = 0
        self._sticky_tried = False
        self._set_state(SmartFPSState.DISCOVERING, "retry after pause")
        return self._handle_discovering()

    # ─── 辅助：切换源 ───

    def _switch_source(self) -> float | None:
        """从当前源切换到下一个可用源"""
        old_name = self._active_name
        self._active_source = None
        self._active_name = None
        self._consecutive_failures = 0

        for name, src in self._sources:
            if name == old_name:
                continue
            if name in self._blacklist:
                continue
            result = src.read()
            if result.state == FPSState.READY:
                self._active_source = src
                self._active_name = name
                self._set_state(SmartFPSState.ACTIVE,
                                f"switched to {name}")
                logging.info("FPS 数据源切换: %s → %s", old_name, name)
                return result.fps
            elif result.state == FPSState.WARMUP:
                # 进入 PENDING 等待
                self._pending_source = src
                self._pending_name = name
                self._pending_since = time.monotonic()
                self._set_state(SmartFPSState.PENDING,
                                f"switched to {name} (warmup)")
                return None
            elif result.state == FPSState.UNSUPPORTED:
                self._blacklist.add(name)

        # 所有源都不可用
        self._set_state(SmartFPSState.PAUSED, "switch failed, all sources exhausted")
        self._pause_time = time.monotonic()
        return None