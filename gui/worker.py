"""独立 Worker 线程 — 每种数据源一个独立线程，各自独立频率采集

FPS:      5Hz  (0.2s)
CPU:      1Hz  (1s)
温度:     0.5Hz (2s)
功耗:     0.2Hz (5s)
内存:     0.2Hz (5s)
网络:     0.5Hz (2s)
"""

import logging
import threading
import time
from dataclasses import dataclass
from PyQt6.QtCore import QThread, pyqtSignal

from core.fps_sources import SmartFPSSource


@dataclass(slots=True)
class FPSUpdate:
    """FPS 采样结果，用于 FPSWorker → MainWindow 信号传递"""
    fps: float
    avg: float
    fps_min: float
    fps_max: float
    t: float
    count: int
    source_name: str = ""


# ─── 基础 Worker ─────────────────────────

class BaseWorker(QThread):
    """带 sleep 循环的基础 Worker"""
    status_update = pyqtSignal(str)
    ready = pyqtSignal()  # 预热完成信号

    def __init__(self, interval: float):
        super().__init__()
        self.interval = interval
        self._warmup_interval = 0.3  # 预热阶段加速轮询间隔
        self._stop_event = threading.Event()
        self.start_time = 0.0
        self.is_ready = False
        self._warmup_data = None  # 缓存预热阶段最后一次有效数据
        self._warmup_timeout = 5.0  # 预热超时秒数

    def run(self) -> None:
        self._run_start = time.monotonic()
        try:
            self.poll()
        except Exception:
            logging.exception("%s warmup poll failed", self.__class__.__name__)
        while not self._stop_event.is_set():
            # 预热阶段使用加速间隔，就绪后恢复正常间隔
            sleep_time = self._warmup_interval if not self.is_ready else self.interval
            # wait() 在事件 set 时立即返回，不会阻塞退出
            if self._stop_event.wait(sleep_time):
                break
            # 预热超时：即使没拿到数据也标记为就绪
            if not self.is_ready:
                elapsed = time.monotonic() - self._run_start
                if elapsed >= self._warmup_timeout:
                    self.is_ready = True
                    self.ready.emit()
            try:
                self.poll()
            except Exception:
                logging.exception("%s poll failed", self.__class__.__name__)

    def poll(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._stop_event.set()

    def get_warmup_data(self):
        """返回预热阶段缓存的最后一次有效数据，由子类决定格式"""
        return self._warmup_data


# ─── FPS Worker (5Hz) — 唯一有统计逻辑的 Worker ───

class FPSWorker(BaseWorker):
    fps_ready = pyqtSignal(object)

    def __init__(self, fps_src: SmartFPSSource, interval: float = 0.2):
        super().__init__(interval)
        self.fps_src = fps_src
        self.fps_min = float('inf')
        self.fps_max = 0.0
        self.fps_sum = 0.0
        self.fps_count = 0
        self._consecutive_failures = 0

    def poll(self) -> None:
        fps = self.fps_src.read_fps()
        if fps is None:
            # 追踪连续失败
            self._consecutive_failures += 1
            if self._consecutive_failures == 30:
                self.status_update.emit("disconnected")
            return
        # 成功读取，恢复连接状态
        if self._consecutive_failures >= 30:
            self.status_update.emit("reconnected")
        self._consecutive_failures = 0
        if not self.is_ready:
            self._warmup_data = fps  # 缓存预热数据
            self.is_ready = True
            self.ready.emit()
            return
        t = time.monotonic() - self.start_time
        self.fps_count += 1
        self.fps_sum += fps
        self.fps_min = min(self.fps_min, fps)
        self.fps_max = max(self.fps_max, fps)
        avg = round(self.fps_sum / self.fps_count, 1)
        src = self.fps_src.active_source_name or ""
        self.fps_ready.emit(FPSUpdate(fps, avg, self.fps_min, self.fps_max, t, self.fps_count, src))

    def reset_time(self, start_time: float) -> None:
        self.start_time = start_time
        self.fps_min = float('inf')
        self.fps_max = 0.0
        self.fps_sum = 0.0
        self.fps_count = 0


# ─── 泛型传感器 Worker (CPU/温度/功耗/内存/网络) ───

class GenericSensorWorker(BaseWorker):
    """通用传感器 Worker：读取 → 缓存 → 发射信号，适用于所有 dict 返回型 reader"""
    data_ready = pyqtSignal(float, dict)

    def __init__(self, reader, interval: float):
        super().__init__(interval)
        self._reader = reader

    def poll(self) -> None:
        data = self._reader.read()
        if not data:
            return
        if not self.is_ready:
            self._warmup_data = dict(data)
            self.is_ready = True
            self.ready.emit()
            return
        self.data_ready.emit(time.monotonic() - self.start_time, data)

    def reset_time(self, start_time: float) -> None:
        self.start_time = start_time


# ─── 设备信息 Worker ──────────────────────

class DeviceInfoWorker(QThread):
    """后台线程获取设备信息，避免阻塞 GUI"""
    finished = pyqtSignal(dict)

    def __init__(self, adb):
        super().__init__()
        self.adb = adb

    def run(self) -> None:
        try:
            from core.adb import get_device_info
            info = {}
            brand, model = get_device_info(self.adb.serial)
            if self.isInterruptionRequested():
                self.finished.emit({})
                return
            info["brand"] = brand
            info["model"] = model

            # 逐个读取设备属性（getprop 不支持多参数批量输出）
            PROP_MAP = {
                "ro.product.device": "device",
                "ro.build.version.release": "android",
                "ro.build.version.sdk": "sdk",
                "ro.hardware.chipname": "chipname",
                "ro.board.platform": "platform",
                "ro.soc.model": "soc_model",
                "ro.hardware": "hardware",
                "ro.hardware.vulkan": "gpu_vulkan",
            }
            cmd = ";".join(f"getprop {p}" for p in PROP_MAP)
            out, _ = self.adb.run_shell(cmd, timeout=10)
            if self.isInterruptionRequested():
                self.finished.emit({})
                return

            if out:
                lines = [l.strip() for l in out.strip().split("\n")]
                for i, key in enumerate(PROP_MAP.values()):
                    if i < len(lines) and lines[i]:
                        info[key] = lines[i]

            # SoC 显示名：优先 chipname → soc_model → platform
            info["soc"] = info.get("chipname") or info.get("soc_model") or info.get("platform") or ""
            # GPU 显示名：优先 vulkan → hardware
            raw_gpu = info.get("gpu_vulkan") or info.get("hardware") or ""
            gpu_map = {"qcom": "Adreno", "mali": "Mali", "adreno": "Adreno"}
            info["gpu"] = gpu_map.get(raw_gpu.lower(), raw_gpu.capitalize()) if raw_gpu else ""

            # CPU 核数 + 各簇核心数（复用 cpufreq policy 结构）
            # nproc 不可用时回退到 /proc/cpuinfo
            out, _ = self.adb.run_shell(
                "nproc --all 2>/dev/null || grep -c '^processor' /proc/cpuinfo; "
                "(for p in /sys/devices/system/cpu/cpufreq/policy*/; do "
                "wc -w < ${p}related_cpus 2>/dev/null; done) 2>/dev/null",
                timeout=5
            )
            if self.isInterruptionRequested():
                self.finished.emit({})
                return

            if out:
                lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
                cpu_cores = lines[0] if lines else ""
                info["cpu_cores"] = cpu_cores
                # wc -w 直接输出每个簇的核心数
                cluster_counts = [line for line in lines[1:] if line.isdigit()]
                if cluster_counts:
                    info["cpu_text"] = f"{cpu_cores} 核 ({'+'.join(cluster_counts)})"
                elif cpu_cores:
                    info["cpu_text"] = f"{cpu_cores} 核"

            # 内存
            out, _ = self.adb.run_shell("head -1 /proc/meminfo", timeout=3)
            if out:
                try:
                    kb = int(out.strip().split()[1])
                    info["ram_text"] = f"{round(kb / 1048576)} GB"
                except (ValueError, IndexError):
                    pass

            self.finished.emit(info)
        except Exception:
            logging.exception("DeviceInfoWorker failed")
            self.finished.emit({"brand": self.adb.serial or "未知设备"})