#!/usr/bin/env python3
"""
ADB 设备诊断脚本 — 收集所有 FPS Monitor 需要的 ADB 数据
用于适配新设备，生成 log 文件
"""

import sys
import time
import re

from core.adb import ADBRunner, get_device_info, _ADB_PATH


def adb(cmd, adb_runner, timeout=10):
    out, _ = adb_runner.run_shell(cmd, timeout=timeout)
    return out


def section(title):
    return f"\n{'='*70}\n  {title}\n{'='*70}"


def main():
    import subprocess
    import argparse

    parser = argparse.ArgumentParser(description="ADB 设备诊断脚本")
    parser.add_argument("-s", "--serial", type=str, default=None, help="设备序列号")
    args = parser.parse_args()
    serial = args.serial

    # 检查设备
    runner = ADBRunner(serial=serial)
    devices = runner.check_device()
    if not devices:
        print("错误: 未检测到 ADB 设备")
        return
    if serial and serial not in devices:
        print(f"错误: 设备 {serial} 未找到，可用: {devices}")
        return
    if not serial:
        runner.serial = devices[0]
        runner._base_cmd = [_ADB_PATH, "-s", devices[0]]
        print(f"自动选择: {devices[0]}")
    serial = runner.serial

    # 提前读取设备名用于文件名
    brand, model = get_device_info(serial)
    device_name = f"{brand}_{model}" if model else serial

    log = []
    log.append("ADB FPS Monitor 设备诊断报告")
    log.append(f"设备: {serial} ({brand} {model})")
    log.append(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log.append(f"Python: {sys.version}")

    # 1. 设备基础信息
    log.append(section("1. 设备基础信息"))
    for cmd in [
        "getprop ro.product.model",
        "getprop ro.product.brand",
        "getprop ro.product.device",
        "getprop ro.build.version.release",
        "getprop ro.build.version.sdk",
        "getprop ro.build.display.id",
        "getprop ro.hardware.chipname",
        "getprop ro.board.platform",
        "getprop ro.product.cpu.abi",
    ]:
        val = adb(cmd, runner)
        log.append(f"  {cmd} = {val}")

    # 2. 前台应用
    log.append(section("2. 前台应用"))
    pkg = runner.get_foreground_package()
    if pkg:
        log.append(f"  前台应用: {pkg}")

    # 3. SurfaceFlinger 窗口列表
    log.append(section("3. SurfaceFlinger --list (SurfaceView 相关)"))
    out = adb("dumpsys SurfaceFlinger --list", runner)
    sv_lines = [l.strip() for l in out.split("\n") if "SurfaceView" in l]
    if sv_lines:
        for l in sv_lines:
            log.append(f"  {l}")
    else:
        log.append("  [无 SurfaceView 窗口]")

    # 4. SurfaceFlinger --latency 测试
    log.append(section("4. SurfaceFlinger --latency 测试"))
    vsync = adb("dumpsys SurfaceFlinger --latency", runner)
    log.append("  --latency (无窗口名):")
    for l in vsync.split("\n")[:3]:
        log.append(f"    {l}")

    if sv_lines:
        match = re.search(r"(SurfaceView\[[^\]]+\](?:\(BLAST\))?(?:#\d+)?)", sv_lines[0])
        if match:
            win = match.group(1)
            safe_win = win.replace("'", "'\\''")
            out = adb(f"dumpsys SurfaceFlinger --latency '{safe_win}'", runner)
            log.append(f"  --latency '{win}':")
            for l in out.split("\n")[:3]:
                log.append(f"    {l}")

    # 5. Buffer frame 计数
    log.append(section("5. Buffer frame 计数 (state=ACQUIRED)"))
    out = adb("dumpsys SurfaceFlinger", runner, timeout=15)
    acq_lines = [l.strip() for l in out.split("\n") if "state=ACQUIRED" in l]
    for l in acq_lines[:5]:
        log.append(f"  {l}")
    if not acq_lines:
        log.append("  [无 ACQUIRED 帧]")

    # 6. gfxinfo
    log.append(section("6. dumpsys gfxinfo (当前前台应用)"))
    if pkg:
        out = adb(f"dumpsys gfxinfo {pkg}", runner)
        for l in out.split("\n")[:15]:
            log.append(f"  {l}")

    # 7. 温度 - thermalservice
    log.append(section("7. dumpsys thermalservice (HAL 温度)"))
    out = adb("dumpsys thermalservice", runner, timeout=5)
    temp_lines = []
    in_current = False
    for line in out.split("\n"):
        line = line.strip()
        if "Current temperatures from HAL:" in line:
            in_current = True
            continue
        if in_current and ("CoolingDevice" in line or "Temperature static" in line):
            break
        if in_current and "Temperature{" in line:
            temp_lines.append(line)
    if temp_lines:
        log.append(f"  找到 {len(temp_lines)} 个 HAL 温度传感器:")
        for l in temp_lines[:30]:
            log.append(f"    {l}")
    else:
        log.append("  [无 HAL 温度数据]")

    # 8. 温度 - sysfs thermal_zone
    log.append(section("8. sysfs thermal_zone"))
    out = adb("ls -d /sys/class/thermal/thermal_zone*", runner, timeout=5)
    zones = [z.strip() for z in out.split("\n") if "thermal_zone" in z]
    log.append(f"  找到 {len(zones)} 个 thermal_zone")

    if zones:
        cmd_parts = []
        for z in zones:
            cmd_parts.append(f"cat {z}/type")
            cmd_parts.append(f"cat {z}/temp")
        out = adb(";".join(cmd_parts), runner, timeout=10)
        lines_out = [l.strip() for l in out.split("\n") if l.strip()]
        log.append("  所有 thermal_zone 类型和温度:")
        for i in range(0, len(lines_out) - 1, 2):
            log.append(f"    zone{i//2:3d}: {lines_out[i]:25s} = {lines_out[i+1]}")

    # 9. 温度 - battery
    log.append(section("9. dumpsys battery (电池温度+电量)"))
    out = adb("dumpsys battery", runner)
    for l in out.split("\n"):
        l = l.strip()
        if any(kw in l.lower() for kw in ["temperature", "level", "voltage", "charge", "status", "powered"]):
            log.append(f"  {l}")

    # 10. CPU 频率
    log.append(section("10. CPU 频率 (cpufreq policies)"))
    out = adb("ls /sys/devices/system/cpu/cpufreq/", runner)
    policies = sorted(re.findall(r"policy(\d+)", out))
    log.append(f"  Policies: {policies}")
    for pid in policies:
        freq = adb(f"cat /sys/devices/system/cpu/cpufreq/policy{pid}/scaling_cur_freq", runner)
        cpus = adb(f"cat /sys/devices/system/cpu/cpufreq/policy{pid}/related_cpus", runner)
        log.append(f"  policy{pid}: related_cpus={cpus.strip()}  cur_freq={freq}")

    # 10b. Per-core CPU 频率
    log.append(section("10b. Per-core CPU 频率"))
    out = adb("ls /sys/devices/system/cpu/", runner)
    cores = sorted([int(m.group(1)) for m in re.finditer(r'\bcpu(\d+)\b', out)]) if out else []
    log.append(f"  CPU cores: {cores}")
    if cores:
        cmds = "; ".join(
            f"cat /sys/devices/system/cpu/cpu{n}/cpufreq/scaling_cur_freq 2>/dev/null"
            for n in cores
        )
        out = adb(cmds, runner)
        lines_out = [l.strip() for l in out.strip().split("\n") if l.strip()]
        for i, core_id in enumerate(cores):
            val = lines_out[i] if i < len(lines_out) else "?"
            freq_mhz = int(val) // 1000 if val.isdigit() else "?"
            log.append(f"  Core{core_id}: {val} ({freq_mhz} MHz)")

    # 10c. CPU 负载 (/proc/stat)
    log.append(section("10c. CPU 负载 (/proc/stat)"))
    out = adb("cat /proc/stat", runner)
    for line in out.split("\n"):
        parts = line.strip().split()
        if len(parts) < 5:
            continue
        if parts[0] == "cpu" or (parts[0].startswith("cpu") and parts[0][3:].isdigit()):
            log.append(f"  {parts[0]:6s}  user={parts[1]}  nice={parts[2]}  sys={parts[3]}  idle={parts[4]}")

    # 11. GPU 频率/负载
    log.append(section("11. GPU 频率/负载"))
    gpu_paths = [
        # 频率路径
        ("gpufreq (devfreq)", "cat /sys/class/devfreq/gpufreq/cur_freq"),
        ("kgsl", "cat /sys/class/kgsl/kgsl-3d0/cur_freq"),
        ("kgsl devfreq", "cat /sys/class/kgsl/kgsl-3d0/devfreq/cur_freq"),
        ("kgsl platform", "cat /sys/devices/platform/soc/3d00000.qcom,kgsl-3d0/devfreq/3d00000.qcom,kgsl-3d0/cur_freq"),
        ("kernel gpu_clock", "cat /sys/kernel/gpu/gpu_clock"),
        ("mali devfreq", "cat /sys/devices/platform/gpufreq/devfreq/gpufreq/cur_freq"),
        # 骁龙负载路径
        ("kgsl gpubusy", "cat /sys/class/kgsl/kgsl-3d0/gpubusy"),
        ("kgsl load %", "cat /sys/class/kgsl/kgsl-3d0/gpu_busy_percentage"),
        ("kgsl devfreq load", "cat /sys/class/kgsl/kgsl-3d0/devfreq/gpu_load"),
        # Mali legacy
        ("mali legacy util", "cat /sys/class/misc/mali0/device/utilization"),
        # Mali devfreq
        ("gpufreq load", "cat /sys/class/devfreq/gpufreq/load"),
        ("gpufreq gpu_busy", "cat /sys/class/devfreq/gpufreq/gpu_busy"),
        ("gpufreq usage", "cat /sys/class/devfreq/gpufreq/gpufreq_usage"),
        # 其他回退
        ("kernel gpu_busy", "cat /sys/kernel/gpu/gpu_busy"),
        ("mali load", "cat /sys/devices/platform/gpufreq/devfreq/gpufreq/load"),
    ]
    for name, cmd in gpu_paths:
        val = adb(cmd, runner)
        if val.strip():
            if " " in val.strip():
                parts = val.strip().split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    status = "✓" if int(parts[1]) > 0 else "✗"
                else:
                    status = "✓"
            elif val.strip().replace("-", "").isdigit():
                status = "✓"
            else:
                status = "?"
        else:
            status = "✗"
        log.append(f"  [{status}] {name:25s} = {val}")

    # 11b. Mali devfreq 自动搜索
    devfreq_out = adb("ls /sys/class/devfreq/", runner)
    mali_devs = [d for d in devfreq_out.strip().split() if "mali" in d.lower()]
    if mali_devs:
        log.append(f"  Mali devfreq 设备: {mali_devs}")
        for dev in mali_devs:
            for suffix in ["gpu_busy", "load"]:
                val = adb(f"cat /sys/class/devfreq/{dev}/{suffix}", runner)
                log.append(f"    /sys/class/devfreq/{dev}/{suffix} = {val}")

    # 11c. getprop GPU 相关
    log.append(section("11c. getprop GPU 相关"))
    out = adb("getprop", runner)
    gpu_props = [l for l in out.split("\n") if any(kw in l.lower() for kw in ["gpu", "mali", "graphic"])]
    for l in gpu_props:
        log.append(f"  {l.strip()}")

    # 12. GPU 显存
    log.append(section("12. dumpsys gpu --gpumem"))
    out = adb("dumpsys gpu --gpumem", runner)
    for l in out.split("\n")[:10]:
        log.append(f"  {l}")

    # 13. 内存 (PSS)
    log.append(section("13. dumpsys meminfo (当前应用)"))
    if pkg:
        out = adb(f"dumpsys meminfo {pkg}", runner, timeout=8)
        for l in out.split("\n"):
            if "TOTAL PSS:" in l or "TOTAL RSS:" in l or "TOTAL SWAP" in l:
                log.append(f"  {l.strip()}")

    # 14. 功耗数据
    log.append(section("14. 功耗数据"))
    # 14a. sysfs 电池数据
    log.append("  [sysfs 电池数据]")
    sysfs_paths = [
        ("voltage_now", "cat /sys/class/power_supply/battery/voltage_now 2>/dev/null"),
        ("capacity", "cat /sys/class/power_supply/battery/capacity 2>/dev/null"),
        ("status", "cat /sys/class/power_supply/battery/status 2>/dev/null"),
        ("charge_counter", "cat /sys/class/power_supply/battery/charge_counter 2>/dev/null"),
        ("current_now", "cat /sys/class/power_supply/battery/current_now 2>/dev/null"),
    ]
    for name, cmd in sysfs_paths:
        val = adb(cmd, runner)
        log.append(f"    {name:20s} = {val}")

    # 14b. dumpsys battery
    log.append("  [dumpsys battery]")
    out = adb("dumpsys battery", runner)
    for l in out.split("\n"):
        l = l.strip()
        if "Charge counter" in l or "voltage:" in l or "level:" in l or "status:" in l or "temperature:" in l:
            log.append(f"    {l}")
    log.append("  (连续读取 charge_counter 5 次，间隔 1 秒)")
    prev = None
    for i in range(5):
        out = adb("dumpsys battery", runner)
        cc = vt = ""
        for l in out.split("\n"):
            if "Charge counter:" in l:
                cc = l.strip()
            elif l.strip().startswith("voltage:"):
                vt = l.strip()
        changed = " <-- CHANGED" if prev and cc != prev else ""
        log.append(f"    [{i}] {cc}  {vt}{changed}")
        prev = cc
        if i < 4:
            time.sleep(1)

    # 15. devfreq 列表 + Mali 平台设备搜索
    log.append(section("15. /sys/class/devfreq/ + Mali 平台设备"))
    out = adb("ls /sys/class/devfreq/", runner)
    for l in out.split("\n"):
        log.append(f"  {l}")

    # 搜索 Mali GPU 平台设备
    log.append("\n  [Mali GPU 平台设备搜索]")
    out = adb("find /sys/devices/platform -name 'misc' -path '*gpu*' 2>/dev/null | head -5", runner)
    if out.strip():
        for mali_misc in out.strip().split("\n"):
            mali_misc = mali_misc.strip()
            log.append(f"  Mali misc 目录: {mali_misc}")
            device_dir = f"{mali_misc}/device"
            out2 = adb(f"ls {device_dir} 2>/dev/null | head -30", runner)
            if out2.strip():
                for item in out2.strip().split("\n"):
                    log.append(f"    {item.strip()}")
            # 尝试读取 utilization
            val = adb(f"cat {device_dir}/utilization 2>&1", runner)
            log.append(f"    utilization = {val}")
            val = adb(f"cat {device_dir}/gpuinfo 2>&1", runner)
            log.append(f"    gpuinfo = {val}")
    else:
        log.append("  [未找到 Mali GPU 平台设备]")

    # 15b. 网络接口
    log.append(section("15b. 网络接口 (/proc/net/dev)"))
    out = adb("cat /proc/net/dev", runner)
    for l in out.strip().split("\n")[:15]:
        log.append(f"  {l}")

    # 15c. Mali 内核线程
    log.append(section("15c. Mali 相关内核线程"))
    out = adb("top -b -n 1 2>/dev/null | grep -iE 'mali|kgsl|gpu_service|gpuservice' | head -10", runner)
    if out.strip():
        for l in out.strip().split("\n"):
            log.append(f"  {l.strip()}")
    else:
        log.append("  [无 Mali/kgsl 相关线程]")

    # 16. FPS 数据源测试（timestats）
    log.append(section("16. FPS 数据源测试"))
    adb("dumpsys SurfaceFlinger --timestats -enable", runner)
    time.sleep(1.0)
    out = adb("dumpsys SurfaceFlinger --timestats -dump", runner, timeout=5)
    ts_layers = []
    pending_frames = None
    pending_fps = None
    for line in out.split("\n"):
        line = line.strip()
        if line.startswith("totalFrames"):
            m = re.search(r"(\d+)", line)
            if m:
                pending_frames = int(m.group(1))
            continue
        if line.startswith("averageFPS"):
            m = re.search(r"([\d.]+)", line)
            if m:
                pending_fps = float(m.group(1))
            continue
        if "layerName" in line:
            name = line.split("=", 1)[1].strip() if "=" in line else ""
            if pending_frames is not None and "SurfaceView" in name:
                ts_layers.append((name, pending_frames, pending_fps))
            pending_frames = None
            pending_fps = None
    if ts_layers:
        log.append(f"  timestats: 找到 {len(ts_layers)} 个 SurfaceView layer:")
        for name, f, fps in sorted(ts_layers, key=lambda x: -x[1]):
            log.append(f"    {name[-60:]}  frames={f}  avgFPS={fps}")
    else:
        log.append("  timestats: [无 SurfaceView layer]")
    if pkg:
        out = adb(f"dumpsys gfxinfo {pkg}", runner)
        for line in out.split("\n"):
            if "Total frames rendered:" in line:
                log.append(f"  gfxinfo: {line.strip()}")
                break
    log.append(f"  buffer count: {len(acq_lines)} 个 ACQUIRED 帧")

    # 17. 完整 SurfaceFlinger --list (搜索用)
    log.append(section("17. SurfaceFlinger --list (完整，搜索用)"))
    out = adb("dumpsys SurfaceFlinger --list", runner)
    for l in out.split("\n"):
        log.append(f"  {l}")

    # 写入文件
    log_text = "\n".join(log)
    filename = f"device_diag_{device_name}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(log_text)

    print(log_text)
    print(f"\n\n诊断报告已保存: {filename}")


if __name__ == "__main__":
    main()