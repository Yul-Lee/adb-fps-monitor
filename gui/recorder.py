"""CSV 录制模块 — 负责 FPS 监控数据的录制和保存"""

import csv
import logging
import time
from datetime import datetime

_FLUSH_INTERVAL = 30  # 每写入 N 行 flush 一次，避免每行刷盘的性能开销


class CSVRecorder:
    """FPS 监控数据 CSV 录制器

    录制列:
    - 基础: 时间(s), FPS, 帧时间(ms), 1%Low, 0.1%Low, Jank
    - 温度: 动态温度传感器列
    - CPU/GPU: CPU负载(%), GPU负载(%), 集群频率, 单核频率, 单核负载
    - 功耗: 功率(mW), 电流(mA), 电压(V), 电量(%)
    - 内存: GPU显存(MB), PSS内存(MB)
    - 网络: 下行(KB/s), 上行(KB/s)
    """

    def __init__(self):
        self._recording = False
        self._csv_file = None
        self._csv_writer = None
        self._csv_header_written = False
        self._csv_temp_columns: list[str] = []
        self._csv_freq_columns: list[str] = []  # 集群频率列
        self._csv_core_freq_columns: list[str] = []  # 单核频率列
        self._csv_core_usage_columns: list[str] = []  # 单核负载列
        self._record_start = 0.0
        self._rows_since_flush = 0

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self, last_temps: dict[str, float]) -> str:
        """开始录制，返回文件名"""
        # 先关闭之前的录制（如果有）
        self.stop()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"fps_record_{ts}.csv"
        self._csv_file = open(fname, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)

        self._csv_temp_columns = sorted(last_temps.keys())
        self._csv_freq_columns = []
        self._csv_core_freq_columns = []
        self._csv_core_usage_columns = []
        self._csv_header_written = False
        self._rows_since_flush = 0
        self._recording = True
        self._record_start = time.time()

        logging.info("开始录制: %s", fname)
        return fname

    def stop(self) -> None:
        """停止录制并安全关闭文件"""
        self._recording = False
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except OSError:
                logging.exception("关闭 CSV 文件失败")
            finally:
                self._csv_file = None
                self._csv_writer = None
                self._csv_header_written = False
                self._csv_temp_columns = []
                self._csv_freq_columns = []
                self._csv_core_freq_columns = []
                self._csv_core_usage_columns = []
                self._rows_since_flush = 0
                logging.info("录制已保存")

    def _update_dynamic_columns(self, last_temps: dict[str, float],
                                last_freqs: dict[str, float]) -> None:
        """更新动态列（温度、频率、单核）"""
        for name in sorted(last_temps):
            if name not in self._csv_temp_columns:
                self._csv_temp_columns.append(name)

        for name, _v in last_freqs.items():
            if name.startswith("Core") and name.endswith("(MHz)") and name not in self._csv_core_freq_columns:
                self._csv_core_freq_columns.append(name)
            elif name.startswith("CPU") and name.endswith("(%)") and name != "CPU负载(%)" and name not in self._csv_core_usage_columns:
                self._csv_core_usage_columns.append(name)
            elif not name.startswith("Core") and not (name.startswith("CPU") and name.endswith("(%)")) and name != "GPU负载(%)" and name not in self._csv_freq_columns:
                self._csv_freq_columns.append(name)

    def _build_header(self) -> list[str]:
        """构建 CSV 表头"""
        header = [
            "时间(s)", "FPS", "帧时间(ms)",
            "1%Low", "0.1%Low", "Jank",
            # CPU/GPU 负载
            "CPU负载(%)", "GPU负载(%)",
        ]
        # 集群频率
        header += self._csv_freq_columns
        # 单核频率
        header += self._csv_core_freq_columns
        # 单核负载
        header += self._csv_core_usage_columns
        # 温度
        header += self._csv_temp_columns
        # 功耗
        header += ["功率(mW)", "电流(mA)", "电压(V)", "电量(%)"]
        # 内存
        header += ["GPU显存(MB)", "PSS内存(MB)"]
        # 网络
        header += ["下行(KB/s)", "上行(KB/s)"]
        return header

    def write_row(self, t: float, fps: float, fps_sorted: list[float],
                  jank_count: int, last_temps: dict[str, float],
                  last_freqs: dict[str, float],
                  last_power: dict[str, float],
                  last_mem: dict[str, float],
                  last_net: dict[str, float]) -> None:
        """写入一行录制数据"""
        if not self._recording or not self._csv_writer:
            return

        # 更新动态列
        self._update_dynamic_columns(last_temps, last_freqs)

        # 首次写入时生成表头
        if not self._csv_header_written:
            self._csv_writer.writerow(self._build_header())
            self._csv_header_written = True

        sorted_fps = sorted(fps_sorted) if fps_sorted else [0]
        n = len(sorted_fps)
        v1 = sorted_fps[max(0, int(n * 0.01))]
        v01 = sorted_fps[max(0, int(n * 0.001))]
        ft = round(1000.0 / fps, 1) if fps > 0.1 else 0

        # 基础 + CPU/GPU 负载
        row = [round(t, 2), fps, ft, v1, v01, jank_count,
               last_freqs.get("CPU负载(%)", 0),
               last_freqs.get("GPU负载(%)", 0)]
        # 集群频率
        for name in self._csv_freq_columns:
            row.append(last_freqs.get(name, 0))
        # 单核频率
        for name in self._csv_core_freq_columns:
            row.append(last_freqs.get(name, 0))
        # 单核负载
        for name in self._csv_core_usage_columns:
            row.append(last_freqs.get(name, 0))
        # 温度
        for name in self._csv_temp_columns:
            row.append(last_temps.get(name, 0))
        # 功耗
        row.append(last_power.get("功率(mW)", 0))
        row.append(last_power.get("电流(mA)", 0))
        row.append(last_power.get("电压(V)", 0))
        row.append(last_power.get("电量(%)", 0))
        # 内存
        row.append(last_mem.get("GPU显存(MB)", 0))
        row.append(last_mem.get("PSS内存(MB)", 0))
        # 网络
        row.append(last_net.get("下行(KB/s)", 0))
        row.append(last_net.get("上行(KB/s)", 0))

        self._csv_writer.writerow(row)
        self._rows_since_flush += 1
        if self._rows_since_flush >= _FLUSH_INTERVAL:
            self._csv_file.flush()
            self._rows_since_flush = 0