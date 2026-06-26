#!/usr/bin/env python3
"""
ADB FPS Monitor - 本地图形化实时监控 FPS/温度/CPU频率
依赖: pip install pyqtgraph PyQt6

使用: python adb_fps_monitor.py [-s 设备] [-p 包名] [-i 间隔] [--no-temp] [--no-freq] [--debug]
"""

import argparse
import sys
import logging

from PyQt6.QtWidgets import QApplication
from gui.main_window import MainWindow


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

    app = QApplication(sys.argv)
    window = MainWindow(
        serial=args.serial,
        package=args.package,
        interval=args.interval,
        no_temp=args.no_temp,
        no_freq=args.no_freq,
    )
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
