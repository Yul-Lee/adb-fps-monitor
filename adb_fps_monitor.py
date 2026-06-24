#!/usr/bin/env python3
"""
ADB FPS Monitor - 本地图形化实时监控 FPS/温度/CPU频率
依赖: pip install pyqtgraph PyQt6

使用: python adb_fps_monitor.py [-s 设备] [-i 间隔] [--no-temp] [--no-freq]
"""

import argparse
import sys
import logging

from core.adb import ADBRunner
from core.fps_sources import SmartFPSSource
from core.sensors import TemperatureReader, FreqReader, PowerReader, MemReader


def main() -> None:
    parser = argparse.ArgumentParser(description="ADB FPS Monitor - 本地图形化实时监控")
    parser.add_argument("-i", "--interval", type=float, default=1.0, help="FPS采样间隔(秒, 最低1.0)")
    parser.add_argument("-s", "--serial", type=str, default=None, help="设备序列号")
    parser.add_argument("-p", "--package", type=str, default=None, help="目标应用包名")
    parser.add_argument("--no-temp", action="store_true", help="关闭温度监控")
    parser.add_argument("--no-freq", action="store_true", help="关闭频率监控")
    parser.add_argument("--debug", action="store_true", help="开启调试日志输出到 adb_fps_debug.log")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(
            filename='adb_fps_debug.log', level=logging.DEBUG,
            format='%(asctime)s %(message)s', force=True
        )
    else:
        logging.basicConfig(level=logging.WARNING)

    adb = ADBRunner(serial=args.serial)
    devices = adb.check_device()
    if not devices:
        print("错误: 未检测到 ADB 设备")
        return
    if not adb.select_device(devices):
        return

    pkg = args.package or adb.get_foreground_package()
    if pkg:
        print(f"前台应用: {pkg}")

    fps_src = SmartFPSSource(adb, package=pkg)
    temp_reader = TemperatureReader(adb) if not args.no_temp else None
    freq_reader = FreqReader(adb) if not args.no_freq else None
    power_reader = PowerReader(adb)
    mem_reader = MemReader(adb, package=pkg)

    # 延迟导入 PyQt6 —— 避免非 GUI 场景下加载图形库
    from PyQt6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication(sys.argv)
    window = MainWindow(adb, fps_src, temp_reader, freq_reader,
                        power_reader, mem_reader, args.interval, pkg)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()