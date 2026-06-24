"""ADB 基础工具 — 设备管理、命令执行、前台包名检测"""

import subprocess
import sys
import os
import re
import time as _time
import logging
from typing import Optional

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
ADB_FLAGS: dict = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _find_adb() -> str:
    """优先使用项目目录下的 platform-tools/adb，否则回退到系统 adb"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        local_adb = os.path.join(project_root, "platform-tools", "adb.exe")
    else:
        local_adb = os.path.join(project_root, "platform-tools", "adb")
    if os.path.isfile(local_adb):
        return local_adb
    return "adb"


_ADB_PATH = _find_adb()


class ADBRunner:
    def __init__(self, serial: Optional[str] = None):
        self.serial = serial
        self._base_cmd: list[str] = [_ADB_PATH]
        if serial:
            self._base_cmd += ["-s", serial]

    def run_shell(self, cmd: str, timeout: int = 8) -> tuple[str, int]:
        full_cmd = self._base_cmd + ["shell", cmd]
        try:
            result = subprocess.run(
                full_cmd, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", **ADB_FLAGS
            )
            return result.stdout, result.returncode
        except subprocess.TimeoutExpired:
            return "", 1
        except Exception:
            return "", 1

    def run_shell_retry(self, cmd: str, timeout: int = 8, retries: int = 2,
                        retry_delay: float = 0.3) -> tuple[str, int]:
        """带重试的 run_shell，高负载时 ADB 可能需要多次尝试"""
        out, rc = "", 1
        for attempt in range(retries + 1):
            out, rc = self.run_shell(cmd, timeout=timeout)
            if rc == 0 and out:
                return out, rc
            if attempt < retries:
                _time.sleep(retry_delay)
        return out, rc

    def check_device(self) -> list[str]:
        try:
            result = subprocess.run(
                [_ADB_PATH, "devices"], capture_output=True, text=True,
                timeout=5, **ADB_FLAGS
            )
            lines = result.stdout.strip().split("\n")
            # 序列号可能包含空格（如三星手表），取除末尾状态外的全部内容
            raw = []
            for l in lines[1:]:
                if not l.strip():
                    continue
                parts = l.split()
                if len(parts) >= 2 and parts[-1] == "device":
                    serial = " ".join(parts[:-1])
                    raw.append(serial)
            if not raw:
                return []
            # 过滤 mDNS/TLS 条目（Android 无线调试的重复连接）
            raw = [d for d in raw if "._adb-tls-connect._tcp" not in d]
            usb = [d for d in raw if not re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", d)]
            tcp = [d for d in raw if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", d)]
            # USB 排前面（推荐），TCP 跟后面
            return usb + tcp
        except Exception:
            return []

    def select_device(self, devices: list[str]) -> bool:
        if self.serial:
            if self.serial in devices:
                return True
            logging.warning("设备 %s 未找到，可用: %s", self.serial, devices)
            return False
        if len(devices) == 1:
            self.serial = devices[0]
            self._base_cmd = [_ADB_PATH, "-s", self.serial]
            logging.info("自动选择设备: %s", self.serial)
            return True
        # 多设备：stdin 可用时用终端选择，否则弹 GUI 对话框
        if sys.stdin.isatty():
            return self._select_device_terminal(devices)
        return self._select_device_gui(devices)

    def _select_device_terminal(self, devices: list[str]) -> bool:
        """终端交互式选择设备"""
        print(f"检测到 {len(devices)} 台设备:")
        for i, d in enumerate(devices):
            print(f"  [{i + 1}] {d}")
        try:
            idx = int(input(f"请选择设备 (1-{len(devices)}): ").strip()) - 1
            if 0 <= idx < len(devices):
                self.serial = devices[idx]
                self._base_cmd = [_ADB_PATH, "-s", self.serial]
                print(f"已选择: {self.serial}")
                return True
            return False
        except (ValueError, EOFError, KeyboardInterrupt):
            return False

    def _select_device_gui(self, devices: list[str]) -> bool:
        """GUI 对话框选择设备（IDE / 无控制台场景）"""
        try:
            from PyQt6.QtWidgets import QApplication, QInputDialog
        except ImportError:
            logging.error("多设备需要选择但无法加载 PyQt6，请用 -s 指定设备序列号")
            return False
        # 可能已有 QApplication（如被外部调用），也可能没有
        app = QApplication.instance() or QApplication(sys.argv)
        items = [f"[{i + 1}] {d}" for i, d in enumerate(devices)]
        item, ok = QInputDialog.getItem(
            None, "ADB FPS Monitor — 选择设备",
            f"检测到 {len(devices)} 台设备:", items, 0, False
        )
        if ok and item:
            idx = items.index(item)
            self.serial = devices[idx]
            self._base_cmd = [_ADB_PATH, "-s", self.serial]
            logging.info("已选择: %s", self.serial)
            return True
        return False

    def get_foreground_package(self) -> Optional[str]:
        out, _ = self.run_shell("dumpsys activity activities")
        for line in out.split("\n"):
            if "mResumedActivity" in line or "topResumedActivity" in line:
                match = re.search(r"u\d+\s+([\w.]+)/", line)
                if match:
                    return match.group(1)
        return None


def get_device_info(serial: Optional[str] = None) -> tuple[str, str]:
    """获取设备基础信息（品牌、型号）"""
    base = [_ADB_PATH]
    if serial:
        base += ["-s", serial]
    try:
        r = subprocess.run(
            base + ["shell", "getprop ro.product.model"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace", **ADB_FLAGS
        )
        model = r.stdout.strip().replace(" ", "_")
        r = subprocess.run(
            base + ["shell", "getprop ro.product.brand"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace", **ADB_FLAGS
        )
        brand = r.stdout.strip().replace(" ", "_")
        return brand, model
    except Exception:
        return "", ""
