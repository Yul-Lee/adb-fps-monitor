"""传感器读取 — 温度 / CPU+GPU频率 / 功耗 / 内存 / 网络"""

import re
import os
import json
import time

# 温度传感器映射配置文件路径
TEMP_MAP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "temp_map.json")


# ─── Temperature Reader ─────────────────────

class TemperatureReader:
    # 第一层：正则规则（按顺序匹配，命中即停）
    TEMP_RULES = [
        # Qualcomm 骁龙 — CPU 小核
        (r"^cpu-0-\d+-usr$", "CPU小核"),
        (r"^cpu-0-\d+$", "CPU小核"),
        (r"^cpu-0-\d+-step$", "CPU小核"),
        (r"^cpu-therm-0-0$", "CPU小核"),
        (r"^apc-0-max-step$", "CPU(小核峰值)"),
        # Qualcomm 骁龙 — CPU 大核/中核
        (r"^cpu-1-[0-3]-usr$", "CPU大核"),
        (r"^cpu-1-[0-3]$", "CPU大核"),
        (r"^cpu-1-[0-3]-step$", "CPU大核"),
        (r"^cpu-1-usr$", "CPU中核"),
        (r"^cpu-therm-0-1$", "CPU中核"),
        (r"^cpu-therm-0-2$", "CPU大核"),
        (r"^apc-1-max-step$", "CPU(大核峰值)"),
        # Qualcomm 骁龙 — CPU 超大核
        (r"^cpu-1-[4-9]-usr$", "CPU超大核"),
        (r"^cpu-2-usr$", "CPU大核"),
        (r"^cpu-3-usr$", "CPU超大核"),
        # Qualcomm 骁龙 — GPU
        (r"^gpuss-0-usr$", "GPU"),
        (r"^gpuss-0$", "GPU"),
        (r"^gpuss-1-usr$", "GPU(传感器1)"),
        (r"^gpuss-1$", "GPU(传感器1)"),
        (r"^gpuss-max-step$", "GPU(峰值)"),
        (r"^gpu-thermal$", "GPU"),
        (r"^gpu-usr$", "GPU"),
        (r"^gpu_step$", "GPU(峰值)"),
        # Qualcomm 骁龙 — CPU 集群
        (r"^cpuss-(\d+)-usr$", "CPU(集群{0})"),
        (r"^cpuss-(\d+)$", "CPU(集群{0})"),
        # Qualcomm 骁龙 — NPU
        (r"^npu-usr$", "NPU"),
        (r"^nspss-(\d+)$", "NPU({0})"),
        # Qualcomm 骁龙 — SoC / AOSS
        (r"^soc-thermal$", "SoC"),
        (r"^soc_max_step$", "SoC(峰值)"),
        (r"^aoss(\d+)-usr$", "AOSS({0})"),
        (r"^aoss-(\d+)$", "AOSS({0})"),
        # Qualcomm 骁龙 — PMIC
        (r"^pm\d+[a-z]*_tz$", "PMIC"),
        # Qualcomm 骁龙 — 其他
        (r"^video$", "视频"),
        (r"^ddr$", "内存"),
        (r"^camera-(\d+)$", "相机({0})"),
        # 通用传感器
        (r"^cpu_therm$", "CPU"),
        (r"^battery$", "电池"),
        (r"^battery_therm$", "电池"),
        (r"^quiet_therm$", "表面"),
        (r"^skin[_-]therm$", "表面"),
        (r"^conn_therm$", "WiFi"),
        (r"^wifi_therm$", "WiFi"),
        (r"^charger_therm\d*$", "充电通路"),
        (r"^chg_therm\d*$", "充电器"),
        (r"^cam_therm$", "相机"),
        (r"^ap_ntc$", "AP(NTC)"),
        (r"^pa_therm\d*$", "射频PA"),
        (r"^flash_therm$", "闪光灯"),
        (r"^backlight_therm$", "背光"),
        (r"^xo_therm$", "XO"),
        # Qualcomm tsens 原始传感器（SoC 内部测温点）
        (r"^tsens_tz_sensor\d+$", "SoC"),
        (r"^case_therm$", "表面"),
        (r"^bms$", "电池"),
    ]

    # 第二层：精确特例（覆盖规则中的通用匹配）
    SPECIAL_MAP = {
        "cpu-therm-0-1": "CPU中核",
        "cpu-therm-0-2": "CPU大核",
        "gpuss-max-step": "GPU(峰值)",
        "soc-thermal": "SoC",
        "battery": "电池",
        "battery_therm": "电池",
        "skin-therm": "表面",
        "skin_therm": "表面",
        "wifi_therm": "WiFi",
        "npu-usr": "NPU",
        "pm8150_tz": "PMIC",
        "pm8150b_tz": "PMIC(B)",
        "pm8150l_tz": "PMIC(L)",
        "pm8350_tz": "PMIC(8350)",
        "pm8350c_tz": "PMIC(8350C)",
        "pm8350b_tz": "PMIC(8350B)",
        "pm8450_tz": "PMIC(8450)",
        "pmr735a_tz": "PMIC(735A)",
        "pmr735b_tz": "PMIC(735B)",
        # Samsung Exynos
        "LITTLE": "CPU小核",
        "BIG": "CPU大核",
        "G3D": "GPU",
    }

    def __init__(self, adb):
        self.adb = adb
        self._user_map = self._load_temp_map()
        self._warmup_cache: dict[str, float] | None = None

    @staticmethod
    def _load_temp_map():
        """加载用户自定义温度映射（可选，存在时覆盖内置规则）"""
        if os.path.isfile(TEMP_MAP_FILE):
            try:
                with open(TEMP_MAP_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {k: v for k, v in data.items() if not k.startswith("_")}
            except Exception:
                pass
        return {}

    def _map_name(self, zone_type: str) -> str | None:
        """三层映射：用户自定义 > 特例 > 正则规则"""
        # 优先用户自定义
        if zone_type in self._user_map:
            return self._user_map[zone_type]
        # 特例
        if zone_type in self.SPECIAL_MAP:
            return self.SPECIAL_MAP[zone_type]
        # 正则规则
        for pattern, alias in self.TEMP_RULES:
            m = re.match(pattern, zone_type)
            if m:
                # 支持 {0} 占位符替换为第一个捕获组
                if m.lastindex:
                    return alias.format(m.group(1))
                return alias
        return None

    def prime(self) -> None:
        self.read()

    def read(self) -> dict[str, float]:
        """读取温度，优先缓存（batch_prime），然后 sysfs，回退 thermalservice，最后 battery"""
        if self._warmup_cache:
            result = self._warmup_cache
            self._warmup_cache = None
            return result
        result = self._read_from_sysfs()
        if result:
            return result
        result = self._read_from_thermalservice()
        if result:
            return result
        return self._read_battery()

    def _read_from_thermalservice(self) -> dict[str, float]:
        out, rc = self.adb.run_shell_retry("dumpsys thermalservice", timeout=5, retries=1)
        if rc != 0 or not out:
            return {}
        temps = {}
        in_current = False
        for line in out.split("\n"):
            line = line.strip()
            if "Current temperatures from HAL:" in line:
                in_current = True
                continue
            if in_current and ("CoolingDevice" in line or "Temperature static" in line):
                break
            if in_current and "Temperature{" in line:
                match = re.search(r"mValue=([\d.]+).*?mName=([\w-]+)", line)
                if match:
                    val = float(match.group(1))
                    name_raw = match.group(2)
                    name = self._map_name(name_raw)
                    if name and -40 < val < 200:
                        if name not in temps:
                            temps[name] = round(val, 1)
        return temps

    def _read_from_sysfs(self) -> dict[str, float]:
        # 用 shell 内建 read 替代 cat，避免每个 zone 产生子进程（3.7s → 0.3s）
        cmd = 'for z in /sys/class/thermal/thermal_zone*; do read t < $z/type 2>/dev/null && read v < $z/temp 2>/dev/null && echo "$t $v"; done'
        out, rc = self.adb.run_shell_retry(cmd, timeout=8, retries=1)
        if rc != 0 or not out:
            return {}

        zone_data = {}
        for line in out.strip().split("\n"):
            parts = line.strip().split()
            if len(parts) >= 2:
                zone_type = parts[0]
                temp_str = parts[1]
                try:
                    val = int(temp_str)
                    if val > 500:
                        val = val / 1000.0
                    if -40 < val < 200:
                        zone_data[zone_type] = val
                except (ValueError, IndexError):
                    pass

        temps = {}
        for zone_type, val in zone_data.items():
            name = self._map_name(zone_type)
            if name:
                if name not in temps or val > temps[name]:
                    temps[name] = round(val, 1)
        return temps

    def _read_battery(self) -> dict[str, float]:
        out, _ = self.adb.run_shell("cat /sys/class/power_supply/battery/temp 2>/dev/null && cat /sys/class/power_supply/battery/status", timeout=3)
        if out:
            lines = out.strip().split("\n")
            if lines and lines[0].isdigit():
                val = int(lines[0])
                return {"电池": val / 1000.0 if val > 200 else val / 10.0}
        # 回退到 dumpsys
        out, _ = self.adb.run_shell("dumpsys battery")
        for line in out.split("\n"):
            if "temperature:" in line:
                match = re.search(r"temperature:\s*(\d+)", line)
                if match:
                    return {"电池": int(match.group(1)) / 10.0}
        return {}


# ─── Power Reader ────────────────────────────

class PowerReader:
    """通过 dumpsys battery 的 charge_counter + voltage 计算实时功耗"""
    def __init__(self, adb):
        self.adb = adb
        self._samples = []
        self._last_result = {"电压(V)": 0, "电量(%)": 0}
        self._bs_cached_ma: float | None = None  # batterystats 缓存的电流值
        self._bs_last_update: float = 0.0         # 上次查询 batterystats 的时间
        self._warmup_cache: dict | None = None

    def prime(self) -> None:
        self.read()

    def read(self) -> dict:
        if self._warmup_cache:
            result = self._warmup_cache
            self._warmup_cache = None
            return result
        # 优先 sysfs 文件读取（轻量快速）
        result = self._read_from_sysfs()
        if result is not None:
            return result
        # 回退到 dumpsys battery
        return self._read_from_dumpsys()

    def _calc_current_from_samples(self, result: dict, is_charging: bool) -> None:
        """从 self._samples 差分计算电流/功率，就地更新 result"""
        if is_charging:
            self._samples.clear()
            return
        now = time.time()
        self._samples = [(t, c, v) for t, c, v in self._samples if now - t <= 30]
        if len(self._samples) < 2:
            return
        oldest, newest = self._samples[0], self._samples[-1]
        dt = newest[0] - oldest[0]
        if dt < 5:
            return
        d_charge = oldest[1] - newest[1]
        current_ma = d_charge * 3600 / dt / 1000
        avg_voltage_mv = (oldest[2] + newest[2]) / 2
        power_mw = current_ma * avg_voltage_mv / 1000
        if -10000 < current_ma < 10000:
            result["电流(mA)"] = round(current_ma, 0)
            result["功率(mW)"] = round(power_mw, 0)

    def _read_from_sysfs(self) -> dict | None:
        """从 sysfs 读取电池数据（单次 adb shell 多文件 cat）"""
        cmd = (
            "cat /sys/class/power_supply/battery/voltage_now 2>/dev/null; "
            "cat /sys/class/power_supply/battery/capacity 2>/dev/null; "
            "cat /sys/class/power_supply/battery/status 2>/dev/null; "
            "cat /sys/class/power_supply/battery/charge_counter 2>/dev/null; "
            "cat /sys/class/power_supply/battery/current_now 2>/dev/null"
        )
        out, rc = self.adb.run_shell(cmd, timeout=3)
        if rc != 0 or not out:
            return None

        lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None

        try:
            voltage_uv = int(lines[0])  # 微伏
            capacity = int(lines[1])    # 百分比
            status_str = lines[2]       # Charging/Discharging/Full/Not charging
            charge_uah = int(lines[3]) if len(lines) > 3 and lines[3].lstrip('-').isdigit() else None
            current_ua = int(lines[4]) if len(lines) > 4 and lines[4].lstrip('-').isdigit() else None
        except (ValueError, IndexError):
            return None

        voltage_v = voltage_uv / 1_000_000.0
        is_charging = status_str.lower() in ("charging", "full")

        result = {
            "电压(V)": round(voltage_v, 2),
            "电量(%)": capacity,
            "充电中": is_charging,
        }

        # 电流：优先 current_now（微安），否则用 charge_counter 差值计算
        if current_ua is not None and not is_charging:
            current_ma = abs(current_ua) / 1000.0
            if 0 < current_ma < 10000:
                result["电流(mA)"] = round(current_ma, 0)
                result["功率(mW)"] = round(current_ma * voltage_v, 0)
        elif charge_uah is not None:
            self._samples.append((time.time(), charge_uah, voltage_uv))
            self._calc_current_from_samples(result, is_charging)
        else:
            self._calc_current_from_samples(result, is_charging)

        self._last_result = result
        return result

    def _read_from_dumpsys(self) -> dict:
        """从 dumpsys battery 读取（回退方案）"""
        out, _ = self.adb.run_shell_retry("dumpsys battery", timeout=5, retries=1)
        if not out:
            return self._last_result

        charge = voltage = level = status = None
        for line in out.split("\n"):
            line = line.strip()
            if "Charge counter:" in line:
                match = re.search(r"(\d+)", line)
                if match:
                    charge = int(match.group(1))
            elif line.startswith("voltage:"):
                match = re.search(r"(\d+)", line)
                if match:
                    voltage = int(match.group(1))
            elif line.startswith("level:"):
                match = re.search(r"(\d+)", line)
                if match:
                    level = int(match.group(1))
            elif line.startswith("status:"):
                match = re.search(r"(\d+)", line)
                if match:
                    status = int(match.group(1))

        if charge is None or voltage is None:
            return self._last_result

        is_charging = status in (2, 5)

        result = {
            "电压(V)": round(voltage / 1000.0, 2),
            "电量(%)": level or 0,
            "充电中": is_charging,
        }

        if not is_charging:
            self._samples.append((time.time(), charge, voltage))
        self._calc_current_from_samples(result, is_charging)

        # 回退：charge_counter 静态（MIUI 等）时，尝试 batterystats 历史
        if result.get("电流(mA)", 0) == 0 and not is_charging:
            self._try_batterystats_current(result)

        self._last_result = result
        return result

    def _try_batterystats_current(self, result: dict) -> None:
        """从 dumpsys batterystats 历史解析最近的 charge 变化来估算电流。

        某些设备（MIUI 等）的 dumpsys battery charge_counter 是静态值，
        但 batterystats 历史中记录了每次 charge 变化。
        结果缓存 60 秒，避免频繁调用重量级命令。
        """
        now = time.time()
        # 使用缓存值
        if self._bs_cached_ma is not None and now - self._bs_last_update < 60:
            current_ma = self._bs_cached_ma
            voltage_v = result.get("电压(V)", 3.8)
            if current_ma > 0:
                result["电流(mA)"] = round(current_ma, 0)
                result["功率(mW)"] = round(current_ma * voltage_v, 0)
            return

        self._bs_last_update = now

        out, rc = self.adb.run_shell("dumpsys batterystats", timeout=8)
        if rc != 0 or not out:
            return

        # 解析历史中的 charge=NNN 条目
        # 格式示例: "+10s219ms (2) 096 volt=4195 charge=2923"
        #           "+1m00s308ms (2) 096 charge=2920"
        charge_entries = []
        for line in out.split("\n"):
            if "charge=" not in line:
                continue
            match = re.search(r"charge=(\d+)", line)
            if not match:
                continue
            charge_val = int(match.group(1))
            # 解析时间：两种格式
            # 格式1: "+1m00s308ms (2) ..." — 相对时间偏移
            # 格式2: "06-24 11:03:45.175 085 ..." — 绝对时间戳
            t = 0.0
            time_prefix = line.split('(')[0] if '(' in line else line[:30]
            # 先尝试绝对时间戳格式 (MM-DD HH:MM:SS.mmm)
            ts_match = re.search(r'(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})', time_prefix)
            if ts_match:
                # 转为秒数（忽略日期，只用时间差）
                h, m, s = int(ts_match.group(3)), int(ts_match.group(4)), int(ts_match.group(5))
                t = h * 3600 + m * 60 + s
            else:
                # 相对时间偏移格式: +XhXmXs
                for unit, multiplier in [('h', 3600), ('m', 60), ('s', 1)]:
                    um = re.search(rf'(\d+){unit}(?!s)', time_prefix)
                    if um:
                        t += int(um.group(1)) * multiplier
            charge_entries.append((t, charge_val))

        if len(charge_entries) < 2:
            self._bs_cached_ma = 0.0
            return

        # 取最近两条
        t1, c1 = charge_entries[-2]
        t2, c2 = charge_entries[-1]
        dt = t2 - t1
        if dt <= 0:
            self._bs_cached_ma = 0.0
            return

        # charge 单位是 mAh（batterystats 历史格式）
        d_charge = c1 - c2  # 放电时 c1 > c2
        current_ma = d_charge * 3600 / dt  # mAh/s → mA

        self._bs_cached_ma = current_ma if 0 < current_ma < 10000 else 0.0

        voltage_v = result.get("电压(V)", 3.8)
        if self._bs_cached_ma > 0:
            result["电流(mA)"] = round(self._bs_cached_ma, 0)
            result["功率(mW)"] = round(self._bs_cached_ma * voltage_v, 0)


# ─── Memory Reader ───────────────────────────

class MemReader:
    """读取 GPU 显存和应用 PSS 内存

    GPU 显存三级降级策略:
    Level 1 (所有机型): dumpsys meminfo {pkg} → Graphics 行
    Level 2 (高通增强): dumpsys gpu --gpumem → Global total (系统级)
    Level 3 (root): KGSL debugfs (暂不支持)
    """
    def __init__(self, adb, package=None):
        self.adb = adb
        self.package = package or ""
        self._warmup_cache: dict[str, int] | None = None

    def prime(self) -> None:
        self.read()

    def read(self) -> dict[str, int]:
        if self._warmup_cache:
            result = self._warmup_cache
            self._warmup_cache = None
            return result
        result = {}
        meminfo_out = None

        # ─── 一次 dumpsys meminfo 同时提取 GPU 显存和 PSS ───
        if self.package:
            meminfo_out, rc = self.adb.run_shell_retry(f"dumpsys meminfo {self.package}", timeout=8, retries=1)
            if rc == 0 and meminfo_out:
                # GPU 显存: Graphics 行（最通用）
                gpu_kb = 0
                for line in meminfo_out.split("\n"):
                    ls = line.strip()
                    if ls.startswith("Graphics:"):
                        parts = ls.split()
                        if len(parts) >= 2:
                            try:
                                gpu_kb = int(parts[1])
                            except ValueError:
                                pass
                # 回退: GL mtrack + EGL mtrack
                if gpu_kb == 0:
                    for line in meminfo_out.split("\n"):
                        ls = line.strip()
                        if "GL mtrack" in ls:
                            parts = ls.split()
                            try:
                                gpu_kb += int(parts[2]) if len(parts) > 2 else 0
                            except ValueError:
                                pass
                        elif "EGL mtrack" in ls:
                            parts = ls.split()
                            try:
                                gpu_kb += int(parts[2]) if len(parts) > 2 else 0
                            except ValueError:
                                pass
                if gpu_kb > 0:
                    result["GPU显存(MB)"] = round(gpu_kb / 1024)

                # PSS 内存
                for line in meminfo_out.split("\n"):
                    if "TOTAL PSS:" in line or "Total PSS:" in line:
                        match = re.search(r"([\d,]+)", line)
                        if match:
                            val = int(match.group(1).replace(",", ""))
                            result["PSS内存(MB)"] = round(val / 1024)
                            break
                    elif line.strip().startswith("TOTAL"):
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                result["PSS内存(MB)"] = round(int(parts[1]) / 1024)
                            except ValueError:
                                pass

        # GPU 显存 Level 2: 如果 Level 1 没拿到，尝试 dumpsys gpu --gpumem
        if "GPU显存(MB)" not in result:
            out, rc = self.adb.run_shell_retry("dumpsys gpu --gpumem", timeout=5, retries=1)
            if rc == 0 and out and "Global total" in out:
                match = re.search(r"Global total:\s*(\d+)", out)
                if match:
                    result["GPU显存(MB)"] = round(int(match.group(1)) / 1048576)

        return result


# ─── Frequency Reader ────────────────────────

class FreqReader:
    def __init__(self, adb):
        self.adb = adb
        self._policies = None
        self._gpu_freq_path = None
        self._gpu_load_path = None
        self._probed = False
        self._prev_cpu_total = None
        self._prev_cpu_idle = None
        self._prev_per_core = None  # [(core_id, total, idle), ...]
        self._warmup_cache: dict[str, float] | None = None
        self._per_core_freq_cache = None  # Reset: uses re.finditer to avoid duplicate core IDs

    # ─── 平台定向路径表 ───
    # 根据 SoC 平台选择 GPU 路径，避免盲试所有路径
    _GPU_FREQ_PATHS = {
        "qualcomm": [
            "cat /sys/class/kgsl/kgsl-3d0/cur_freq",
            "cat /sys/class/kgsl/kgsl-3d0/devfreq/cur_freq",
            "cat /sys/devices/platform/soc/3d00000.qcom,kgsl-3d0/devfreq/3d00000.qcom,kgsl-3d0/cur_freq",
            "cat /sys/class/devfreq/gpufreq/cur_freq",
        ],
        "mali": [
            "cat /sys/class/devfreq/gpufreq/cur_freq",
            "cat /sys/kernel/gpu/gpu_clock",
            "cat /sys/devices/platform/gpufreq/devfreq/gpufreq/cur_freq",
        ],
        "generic": [
            "cat /sys/class/devfreq/gpufreq/cur_freq",
            "cat /sys/class/kgsl/kgsl-3d0/cur_freq",
            "cat /sys/kernel/gpu/gpu_clock",
            "cat /sys/devices/platform/gpufreq/devfreq/gpufreq/cur_freq",
        ],
    }
    _GPU_LOAD_PATHS = {
        "qualcomm": [
            ("cat /sys/class/kgsl/kgsl-3d0/gpubusy", True),
            ("cat /sys/class/kgsl/kgsl-3d0/gpu_busy_percentage", False),
            ("cat /sys/class/devfreq/gpufreq/load", True),
            ("cat /sys/class/devfreq/gpufreq/load", False),
        ],
        "mali": [
            ("cat /sys/kernel/gpu/gpu_busy", False),
            ("cat /sys/class/misc/mali0/device/utilization", False),
        ],
        "generic": [
            ("cat /sys/class/kgsl/kgsl-3d0/gpubusy", True),
            ("cat /sys/class/misc/mali0/device/utilization", False),
            ("cat /sys/class/devfreq/gpufreq/load", True),
            ("cat /sys/kernel/gpu/gpu_busy", False),
        ],
    }

    def _detect_platform(self) -> str:
        """1 次 ADB 调用识别 GPU 平台，返回 'qualcomm'/'mali'/'generic'"""
        cmd = (
            "getprop ro.board.platform; getprop ro.hardware.chipname; "
            "getprop ro.hardware; getprop ro.soc.model"
        )
        out, _ = self.adb.run_shell(cmd, timeout=3)
        raw = out.lower().strip() if out else ""
        if any(k in raw for k in ["qcom", "msm", "sdm", "sm8", "sm7", "sm6", "sm4", "kona", "lahaina", "taro", "kalama", "pineapple"]):
            return "qualcomm"
        if any(k in raw for k in ["exynos", "mali", "samsung"]):
            return "mali"
        if any(k in raw for k in ["mtk", "mediatek", "dimensity", "mt6"]):
            return "mali"  # MTK 也使用 Mali GPU
        return "generic"

    def _try_gpu_load(self, path: str, is_ratio: bool) -> bool:
        """尝试读取 GPU 负载路径，成功返回 True"""
        f, r = self.adb.run_shell(path)
        if r != 0 or not f.strip():
            return False
        raw = f.strip()
        parts = raw.split()
        try:
            if is_ratio and len(parts) >= 2:
                busy, total = int(parts[0]), int(parts[1])
                if total > 0 and 0 <= busy <= total:
                    self._gpu_load_path = path
                    self._gpu_busy_is_ratio = True
                    return True
            else:
                # 处理 "0%" 格式（如 Exynos /sys/kernel/gpu/gpu_busy）
                val_str = parts[0].rstrip('%')
                v = int(val_str)
                if 0 <= v <= 100:
                    self._gpu_load_path = path
                    self._gpu_busy_is_ratio = False
                    return True
        except (ValueError, IndexError):
            pass
        return False

    def _probe(self):
        # ─── 1. CPU policy 探测（2 次 ADB 调用：ls + 批量 cat） ───
        out, rc = self.adb.run_shell("ls /sys/devices/system/cpu/cpufreq/")
        pids = sorted(re.findall(r"policy(\d+)", out)) if rc == 0 and out else []
        policies = []
        if pids:
            # 合并为 1 次 ADB：每个 policy 读取 freq + related_cpus
            cmds = "; ".join(
                f"echo __P{pid}; "
                f"cat /sys/devices/system/cpu/cpufreq/policy{pid}/scaling_cur_freq 2>/dev/null; "
                f"cat /sys/devices/system/cpu/cpufreq/policy{pid}/related_cpus 2>/dev/null"
                for pid in pids
            )
            out, _ = self.adb.run_shell(cmds, timeout=5)
            if out:
                blocks = re.split(r"__P(\d+)", out)
                # blocks[0] 为空, blocks[1]=pid, blocks[2]=content, ...
                for i in range(1, len(blocks) - 1, 2):
                    pid = blocks[i]
                    content = blocks[i + 1].strip().split("\n")
                    freq_str = content[0].strip() if content else ""
                    rng = content[1].strip().replace("\n", " ") if len(content) > 1 else ""
                    if freq_str.isdigit():
                        policies.append((len(policies) + 1, pid, rng))
        self._policies = policies

        # ─── 2. 平台识别（1 次 ADB 调用） ───
        platform = self._detect_platform()

        # ─── 3. GPU 频率：定向尝试平台路径 ───
        self._gpu_freq_path = None
        freq_paths = self._GPU_FREQ_PATHS.get(platform, self._GPU_FREQ_PATHS["generic"])
        # 合并为 1 次 ADB 调用：逐路径 cat 用分隔符输出
        probe_cmds = []
        for i, path in enumerate(freq_paths):
            cmd = path.replace("cat ", "")
            probe_cmds.append(f"cat {cmd} 2>/dev/null || echo __FAIL_{i}")
        combined = "; ".join(probe_cmds)
        out, rc = self.adb.run_shell(combined, timeout=5)
        if rc == 0 and out:
            for i, (line, path) in enumerate(zip(out.strip().split("\n"), freq_paths)):
                val = line.strip()
                if val.isdigit() and not val.startswith("__FAIL"):
                    self._gpu_freq_path = path
                    break

        # ─── 4. GPU 负载：定向尝试平台路径 ───
        self._gpu_busy_is_ratio = False
        self._gpu_load_path = None
        load_paths = self._GPU_LOAD_PATHS.get(platform, self._GPU_LOAD_PATHS["generic"])
        for path, is_ratio in load_paths:
            if self._try_gpu_load(path, is_ratio):
                break

        # ─── 5. Mali devfreq 搜索（仅 mali 平台或 generic 回退时） ───
        if not self._gpu_load_path and platform in ("mali", "generic"):
            devfreq_out, dr = self.adb.run_shell("ls /sys/class/devfreq/")
            if dr == 0 and devfreq_out:
                for dev_name in devfreq_out.strip().split():
                    if "mali" in dev_name.lower():
                        for suffix in ["/gpu_busy", "/load"]:
                            mali_path = f"cat /sys/class/devfreq/{dev_name}{suffix}"
                            if self._try_gpu_load(mali_path, False):
                                break
                        if self._gpu_load_path:
                            break

        self._probed = True

    def prime(self) -> None:
        """预热：提前读取一次，建立 CPU 负载基准"""
        self.read()

    def read(self) -> dict[str, float]:
        if self._warmup_cache:
            result = self._warmup_cache
            self._warmup_cache = None
            return result
        if not self._probed:
            self._probe()

        if self._policies:
            cmds = "; ".join(
                f"cat /sys/devices/system/cpu/cpufreq/policy{pid}/scaling_cur_freq 2>/dev/null"
                for _, pid, _ in self._policies
            )
            out, rc = self.adb.run_shell_retry(cmds, timeout=8, retries=1)
            cpu = {}
            if rc == 0 and out:
                lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
                for i, (cid, pid, rng) in enumerate(self._policies):
                    if i < len(lines) and lines[i].isdigit():
                        label = f"CPU{cid}({rng})" if rng else f"CPU{cid}"
                        cpu[label] = int(lines[i]) // 1000
            result = cpu
        else:
            result = {}

        if self._gpu_freq_path or self._gpu_load_path:
            parts = []
            if self._gpu_freq_path:
                parts.append(self._gpu_freq_path)
            if self._gpu_load_path:
                parts.append(self._gpu_load_path)
            out, rc = self.adb.run_shell("; ".join(parts))
            if rc == 0 and out:
                lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
                if self._gpu_freq_path and lines:
                    v_str = lines[0]
                    if v_str.isdigit():
                        v = int(v_str)
                        result["GPU"] = v // 1000000 if v > 1000000 else (v // 1000 if v > 1000 else v)
                if self._gpu_load_path and lines:
                    load_str = lines[1] if (self._gpu_freq_path and len(lines) > 1) else lines[0]
                    try:
                        parts = load_str.strip().split()
                        if self._gpu_busy_is_ratio and len(parts) >= 2:
                            # 骁龙 gpubusy: "busy total" -> 计算百分比
                            busy = int(parts[0])
                            total = int(parts[1])
                            if total > 0:
                                result["GPU负载(%)"] = round(busy / total * 100, 1)
                        else:
                            # 处理 "0%" 格式（如 Exynos /sys/kernel/gpu/gpu_busy）
                            load = int(parts[0].rstrip('%'))
                            if 0 <= load <= 100:
                                result["GPU负载(%)"] = load
                    except (ValueError, IndexError):
                        pass

        # Per-core frequency from sysfs
        per_core_freqs = self._read_per_core_freqs()
        for k, v in per_core_freqs.items():
            result[k] = v

        # CPU usage from /proc/stat
        cpu_usage = self._read_cpu_usage()
        for k, v in cpu_usage.items():
            result[k] = v

        return result

    def _read_per_core_freqs(self) -> dict[str, int]:
        """读取每个 CPU 核心的频率"""
        if self._per_core_freq_cache is None:
            out, rc = self.adb.run_shell("ls /sys/devices/system/cpu/", timeout=3)
            cores = []
            if rc == 0 and out:
                cores = sorted([int(m.group(1)) for m in re.finditer(r'\bcpu(\d+)\b', out)])
            self._per_core_freq_cache = cores

        result = {}
        if not self._per_core_freq_cache:
            return result

        # 批量读取
        cmds = "; ".join(
            f"cat /sys/devices/system/cpu/cpu{n}/cpufreq/scaling_cur_freq 2>/dev/null"
            for n in self._per_core_freq_cache
        )
        out, _ = self.adb.run_shell_retry(cmds, timeout=5, retries=1)
        if out:
            lines = [l.strip() for l in out.strip().split("\n") if l.strip()]
            for i, core_id in enumerate(self._per_core_freq_cache):
                if i < len(lines) and lines[i].isdigit():
                    result[f"Core{core_id}(MHz)"] = int(lines[i]) // 1000
        return result

    def _read_cpu_usage(self) -> dict[str, float]:
        out, rc = self.adb.run_shell("cat /proc/stat", timeout=2)
        if rc != 0 or not out:
            return {}

        result = {}
        cores = []
        for line in out.split("\n"):
            parts = line.strip().split()
            if len(parts) < 5:
                continue
            if parts[0] == "cpu":
                # 综合负载
                try:
                    values = [int(x) for x in parts[1:]]
                except ValueError:
                    continue
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                if self._prev_cpu_total is not None:
                    d_total = total - self._prev_cpu_total
                    d_idle = idle - self._prev_cpu_idle
                    if d_total > 0:
                        result["CPU负载(%)"] = round((1.0 - d_idle / d_total) * 100, 1)
                self._prev_cpu_total = total
                self._prev_cpu_idle = idle
            elif parts[0].startswith("cpu") and parts[0][3:].isdigit():
                core_id = int(parts[0][3:])
                try:
                    values = [int(x) for x in parts[1:]]
                except ValueError:
                    continue
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                cores.append((core_id, total, idle))

        # 单核负载
        if cores:
            new_per_core = []
            for core_id, total, idle in cores:
                new_per_core.append((core_id, total, idle))
                if self._prev_per_core:
                    for prev_cid, prev_total, prev_idle in self._prev_per_core:
                        if prev_cid == core_id:
                            d_total = total - prev_total
                            d_idle = idle - prev_idle
                            if d_total > 0:
                                result[f"CPU{core_id}(%)"] = round((1.0 - d_idle / d_total) * 100, 1)
                            break
            self._prev_per_core = new_per_core

        return result


# ─── Network Reader ──────────────────────────

class NetReader:
    """通过 /proc/net/dev 读取网络流量（上下行速率）"""
    def __init__(self, adb):
        self.adb = adb
        self._prev_rx = None
        self._prev_tx = None
        self._prev_time = None

    def prime(self) -> None:
        self.read()

    def read(self) -> dict[str, float]:
        out, rc = self.adb.run_shell(
            "cat /proc/net/dev", timeout=3
        )
        if rc != 0 or not out:
            return {}

        total_rx = 0
        total_tx = 0
        for line in out.split("\n"):
            line = line.strip()
            if ":" not in line:
                continue
            iface, data = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue
            parts = data.split()
            if len(parts) >= 10:
                try:
                    total_rx += int(parts[0])
                    total_tx += int(parts[8])
                except ValueError:
                    pass

        if total_rx == 0 and total_tx == 0:
            return {}

        now = time.time()
        if self._prev_rx is None:
            self._prev_rx = total_rx
            self._prev_tx = total_tx
            self._prev_time = now
            return {}

        elapsed = now - self._prev_time
        if elapsed < 0.5:
            return {}

        rx_rate = (total_rx - self._prev_rx) / 1024.0 / elapsed
        tx_rate = (total_tx - self._prev_tx) / 1024.0 / elapsed

        self._prev_rx = total_rx
        self._prev_tx = total_tx
        self._prev_time = now

        return {
            "下行(KB/s)": round(max(rx_rate, 0), 1),
            "上行(KB/s)": round(max(tx_rate, 0), 1),
        }


# ─── 批量预热 ──────────────────────────

def batch_prime(adb, temp_reader=None, freq_reader=None,
                power_reader=None, net_reader=None) -> None:
    """一次 ADB 调用预读所有传感器数据，建立基准 + 预热连接 + 缓存首次读数。

    每个 Reader 的 _warmup_cache 被填充后，首次 read() 直接返回缓存，
    无需再发 ADB 调用，消除 Worker 首次 poll 的 ~600ms 延迟。
    """
    parts = []
    # 温度
    parts.append(
        "echo __TEMP__; "
        "for z in /sys/class/thermal/thermal_zone*; do "
        "read t < $z/type 2>/dev/null && read v < $z/temp 2>/dev/null "
        "&& echo \"$t $v\"; done"
    )
    # 功耗
    parts.append(
        "echo __POWER__; "
        "cat /sys/class/power_supply/battery/voltage_now 2>/dev/null; "
        "cat /sys/class/power_supply/battery/capacity 2>/dev/null; "
        "cat /sys/class/power_supply/battery/status 2>/dev/null; "
        "cat /sys/class/power_supply/battery/charge_counter 2>/dev/null; "
        "cat /sys/class/power_supply/battery/current_now 2>/dev/null"
    )
    # CPU 负载基准 + policy 探测 + 集群频率
    if freq_reader:
        parts.append(
            "echo __FREQ__; cat /proc/stat; "
            "ls /sys/devices/system/cpu/cpufreq/; "
            "for p in /sys/devices/system/cpu/cpufreq/policy*/; do "
            "echo __P; cat ${p}scaling_cur_freq 2>/dev/null; "
            "cat ${p}related_cpus 2>/dev/null; done"
        )
    # 内存（dumpsys meminfo 太慢 ~3s，不放在这里，MemReader 自己读）
    # 网络
    parts.append("echo __NET__; cat /proc/net/dev")

    combined = "; ".join(parts)
    out, _ = adb.run_shell(combined, timeout=10)
    if not out:
        return

    # 按标签分段
    sections: dict[str, str] = {}
    current_label = ""
    current_lines: list[str] = []
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith("__") and line.endswith("__"):
            if current_label:
                sections[current_label] = "\n".join(current_lines)
            current_label = line.strip("_").lower()
            current_lines = []
        else:
            current_lines.append(line)
    if current_label:
        sections[current_label] = "\n".join(current_lines)

    # 分发给各 reader
    if temp_reader and "temp" in sections:
        _batch_prime_temp(temp_reader, sections["temp"])
    if power_reader and "power" in sections:
        _batch_prime_power(power_reader, sections["power"])
    if freq_reader and "freq" in sections:
        _batch_prime_freq(freq_reader, sections["freq"])
    if net_reader and "net" in sections:
        _batch_prime_net(net_reader, sections["net"])


def _batch_prime_temp(reader, raw: str) -> None:
    """解析温度数据，设置 _warmup_cache"""
    temps = {}
    for line in raw.strip().split("\n"):
        parts = line.strip().split()
        if len(parts) >= 2:
            zone_type = parts[0]
            try:
                val = int(parts[1])
                if val > 500:
                    val = val / 1000.0
                if -40 < val < 200:
                    name = reader._map_name(zone_type)
                    if name:
                        if name not in temps or val > temps[name]:
                            temps[name] = round(val, 1)
            except (ValueError, IndexError):
                pass
    if temps:
        reader._warmup_cache = temps


def _batch_prime_freq(reader, raw: str) -> None:
    """解析 /proc/stat，建立 CPU 负载基准（含单核）+ 缓存集群频率"""
    freq_result = {}
    per_core = []
    for line in raw.strip().split("\n"):
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "cpu" and len(parts) >= 5:
            try:
                values = [int(x) for x in parts[1:]]
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                reader._prev_cpu_total = total
                reader._prev_cpu_idle = idle
            except (ValueError, IndexError):
                pass
        elif parts[0].startswith("cpu") and parts[0][3:].isdigit() and len(parts) >= 5:
            try:
                core_id = int(parts[0][3:])
                values = [int(x) for x in parts[1:]]
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                total = sum(values)
                per_core.append((core_id, total, idle))
            except (ValueError, IndexError):
                pass
    if per_core:
        reader._prev_per_core = per_core
    # 解析 policy 探测数据（__P 分隔）
    sections = raw.split("__P")
    for sec in sections[1:]:  # 跳过 /proc/stat 部分
        lines = [l.strip() for l in sec.strip().split("\n") if l.strip()]
        if len(lines) >= 2:
            freq_str = lines[0]
            related = lines[1]
            if freq_str.isdigit():
                freq_mhz = int(freq_str) // 1000
                ids = related.split()
                rng = f"{ids[0]}-{ids[-1]}" if len(ids) > 1 else (ids[0] if ids else "")
                label = f"CPU({rng})" if rng else "CPU"
                freq_result[label] = freq_mhz
    if freq_result:
        reader._warmup_cache = freq_result


def _batch_prime_power(reader, raw: str) -> None:
    """解析功耗数据，建立基准 + 设置 _warmup_cache"""
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    if len(lines) < 3:
        return
    try:
        voltage_uv = int(lines[0])
        capacity = int(lines[1]) if lines[1].isdigit() else 0
        status_str = lines[2]
        charge_uah = int(lines[3]) if len(lines) > 3 and lines[3].lstrip('-').isdigit() else None
        current_ua = int(lines[4]) if len(lines) > 4 and lines[4].lstrip('-').isdigit() else None
    except (ValueError, IndexError):
        return

    voltage_v = voltage_uv / 1_000_000.0
    is_charging = status_str.lower() in ("charging", "full")

    result = {
        "电压(V)": round(voltage_v, 2),
        "电量(%)": capacity,
        "充电中": is_charging,
    }
    if current_ua is not None and not is_charging:
        current_ma = abs(current_ua) / 1000.0
        if 0 < current_ma < 10000:
            result["电流(mA)"] = round(current_ma, 0)
            result["功率(mW)"] = round(current_ma * voltage_v, 0)

    reader._warmup_cache = result

    if not is_charging and charge_uah is not None:
        reader._samples.append((time.time(), charge_uah, voltage_uv))


def _batch_prime_net(reader, raw: str) -> None:
    """解析网络数据并建立基准（首次 read 需要两次采样，不缓存）"""
    total_rx = 0
    total_tx = 0
    for line in raw.strip().split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo":
            continue
        parts = data.split()
        if len(parts) >= 10:
            try:
                total_rx += int(parts[0])
                total_tx += int(parts[8])
            except ValueError:
                pass
    if total_rx > 0 or total_tx > 0:
        reader._prev_rx = total_rx
        reader._prev_tx = total_tx
        reader._prev_time = time.time()