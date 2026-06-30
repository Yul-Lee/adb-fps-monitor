"""配套 App 数据读取器 — 通过 HTTP 读取 Android App 采集的功耗/内存/网络数据

通信方式：ADB 端口转发 + HTTP
App 端口：18765（MonitorHttpServer.PORT）
"""

import json
import logging
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

APP_PACKAGE = "com.adbmonitor.companion"
APP_PORT = 18765
HTTP_TIMEOUT = 2


class AppDataReader:
    """通过 HTTP 读取配套 App 数据"""

    def __init__(self, adb):
        self.adb = adb
        self._url_base = f"http://localhost:{APP_PORT}"
        self._available = False

    def start_app(self, package: str) -> None:
        """通过 ADB 启动 Activity（自动启 Service 后立即切后台）"""
        cmd = (
            f"am start -n {APP_PACKAGE}/.MainActivity "
            f"--es target_package '{package}'"
        )
        self.adb.run_shell(cmd, timeout=5)

    def is_running(self) -> bool:
        """通过 ps 检测 App 是否运行"""
        out, rc = self.adb.run_shell(f"pidof {APP_PACKAGE}", timeout=3)
        return rc == 0 and out.strip().isdigit()

    def probe(self) -> bool:
        """检测 App 是否可用：端口转发 + ping"""
        # ADB 端口转发
        out, rc = self.adb.run_host("forward", f"tcp:{APP_PORT}", f"tcp:{APP_PORT}")
        if rc != 0:
            logger.debug("ADB forward failed: %s", out)
            return False

        # HTTP ping
        try:
            url = f"{self._url_base}/api/ping"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read())
                self._available = data.get("status") == "ok"
                return self._available
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            logger.debug("App ping failed: %s", e)
            self._available = False
            return False

    def _fetch(self, kind: str = None) -> dict | None:
        """HTTP GET /api/data[?kind=xxx]"""
        url = f"{self._url_base}/api/data"
        if kind:
            url += f"?kind={kind}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            logger.debug("App fetch failed (%s): %s", kind, e)
            self._available = False
            return None

    def read_power(self) -> dict:
        """读取功耗数据，返回与 PowerReader 兼容的 dict"""
        data = self._fetch("power")
        if not data or "power" not in data:
            return {}
        p = data["power"]
        result = {}
        if "current_mA" in p:
            result["电流(mA)"] = round(p["current_mA"])
        if "voltage_V" in p:
            result["电压(V)"] = round(p["voltage_V"], 2)
        if "power_mw" in p:
            result["功率(mW)"] = round(p["power_mw"])
        if "capacity" in p:
            result["电量(%)"] = p["capacity"]
        return result

    def read_memory(self) -> dict:
        """读取内存数据，返回与 MemReader 兼容的 dict"""
        data = self._fetch("memory")
        if not data or "memory" not in data:
            return {}
        m = data["memory"]
        result = {}
        if "target_pss_mb" in m:
            result["PSS内存(MB)"] = m["target_pss_mb"]
        if "total_mb" in m and "avail_mb" in m:
            result["已用内存(MB)"] = m["total_mb"] - m["avail_mb"]
            result["可用内存(MB)"] = m["avail_mb"]
        return result

    def read_network(self) -> dict:
        """读取网络数据，返回与 NetReader 兼容的 dict"""
        data = self._fetch("network")
        if not data or "network" not in data:
            return {}
        n = data["network"]
        result = {}
        if "rx_rate_kbps" in n:
            result["下行(KB/s)"] = round(n["rx_rate_kbps"])
        if "tx_rate_kbps" in n:
            result["上行(KB/s)"] = round(n["tx_rate_kbps"])
        return result

    @property
    def available(self) -> bool:
        return self._available
