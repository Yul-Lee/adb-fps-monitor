#!/usr/bin/env python3
"""
FPS Debug Tool - 诊断 FPS 数据源在设备上的可用性
用法: python fps_debug.py [-s 设备] [-p 包名]
"""

import time
import re
import sys
import argparse

from core.adb import ADBRunner


def main():
    parser = argparse.ArgumentParser(description="FPS Debug Tool")
    parser.add_argument("-s", "--serial", type=str, default=None)
    parser.add_argument("-p", "--package", type=str, default=None)
    parser.add_argument("-n", "--rounds", type=int, default=3, help="每种方式测试轮数")
    args = parser.parse_args()

    adb = ADBRunner(serial=args.serial)

    # 检查设备
    devices = adb.check_device()
    if not devices:
        print("❌ 未检测到 ADB 设备")
        return
    print(f"✅ 设备: {devices[0] if len(devices) == 1 else ', '.join(devices)}")

    # 检查前台应用
    pkg = args.package or adb.get_foreground_package()
    if pkg:
        print(f"✅ 目标应用: {pkg}")
    else:
        print("⚠️  未指定包名，部分测试将跳过")

    print("=" * 60)

    # ─── 测试 1: SurfaceFlinger buffer count ───
    print("\n📊 [1] SurfaceFlinger buffer frame count")
    print("   原理: 解析 state=ACQUIRED frame=N 的最大 N")
    results_sf = []
    for i in range(args.rounds):
        out, _ = adb.run_shell("dumpsys SurfaceFlinger")
        max_frame = 0
        for line in out.split("\n"):
            if "state=ACQUIRED" in line:
                m = re.search(r"frame=(\d+)", line)
                if m:
                    f = int(m.group(1))
                    if f > max_frame:
                        max_frame = f
        results_sf.append(max_frame)
        print(f"   轮次 {i+1}: max_acquired_frame = {max_frame}")
        time.sleep(1.0)
    if len(results_sf) >= 2:
        delta = results_sf[-1] - results_sf[0]
        dt = (len(results_sf) - 1) * 1.0
        fps = delta / dt if dt > 0 and delta > 0 else 0
        print(f"   → FPS: {fps:.1f} ({'✅ 有效' if fps > 0 else '❌ 无效'})")
    else:
        print("   → ❌ 无数据")

    # ─── 测试 2: SurfaceFlinger --latency ───
    print("\n📊 [2] SurfaceFlinger --latency")
    out, _ = adb.run_shell("dumpsys SurfaceFlinger --list")
    sv_candidates = []
    for line in out.split("\n"):
        line = line.strip()
        if pkg and pkg in line and "SurfaceView" in line:
            sv_candidates.append(line)
        elif "SurfaceView[" in line and "Background for" not in line:
            sv_candidates.append(line)
    if sv_candidates:
        print(f"   找到 {len(sv_candidates)} 个 SurfaceView:")
        for c in sv_candidates[:5]:
            print(f"     - {c}")
        win_match = re.search(r"(SurfaceView\[[^\]]+\](?:\(BLAST\))?(?:#\d+)?)", sv_candidates[0])
        if win_match:
            win = win_match.group(1)
            safe_win = win.replace("'", "'\\''")
            out, _ = adb.run_shell(f"dumpsys SurfaceFlinger --latency '{safe_win}'")
            lines = [l for l in out.strip().split("\n") if l.strip()]
            print(f"   latency 数据行数: {len(lines)}")
            if len(lines) > 1:
                print(f"   → ✅ 有帧数据 (窗口: {win})")
            else:
                print(f"   → ❌ 帧数据为空 (窗口: {win})")
    else:
        print("   → ❌ 未找到 SurfaceView")

    # ─── 测试 3: gfxinfo ───
    if pkg:
        print(f"\n📊 [3] gfxinfo ({pkg})")
        results_gfx = []
        for i in range(args.rounds):
            out, _ = adb.run_shell(f"dumpsys gfxinfo {pkg}")
            total = None
            for line in out.split("\n"):
                if "Total frames rendered:" in line:
                    m = re.search(r"(\d+)", line)
                    if m:
                        total = int(m.group(1))
                        break
            results_gfx.append(total)
            print(f"   轮次 {i+1}: Total frames rendered = {total}")
            time.sleep(1.0)
        if len(results_gfx) >= 2 and all(v is not None for v in results_gfx):
            delta = results_gfx[-1] - results_gfx[0]
            dt = (len(results_gfx) - 1) * 1.0
            fps = delta / dt if dt > 0 and delta > 0 else 0
            print(f"   → FPS: {fps:.1f} ({'✅ 有效' if fps > 0 else '⚠️ 帧数未增长'})")
        else:
            print("   → ❌ 无数据")
    else:
        print("\n📊 [3] gfxinfo - 跳过（无包名）")

    # ─── 测试 4: timestats ───
    print("\n📊 [4] SurfaceFlinger --timestats")
    adb.run_shell("dumpsys SurfaceFlinger --timestats -enable")
    time.sleep(1.0)
    results_ts = []
    for i in range(args.rounds):
        out, _ = adb.run_shell("dumpsys SurfaceFlinger --timestats -dump")
        layers = {}
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
                    if (not pkg) or (pkg in name):
                        layers[name] = (pending_frames, pending_fps)
                pending_frames = None
                pending_fps = None
        if layers:
            for name, (f, fps) in sorted(layers.items(), key=lambda x: -x[1][0]):
                print(f"   轮次 {i+1}: {name[-50:]}  frames={f}  avgFPS={fps}")
            results_ts.append(max(v[0] for v in layers.values()))
        else:
            print(f"   轮次 {i+1}: 无匹配的 SurfaceView layer")
            results_ts.append(None)
        time.sleep(1.0)

    if len(results_ts) >= 2 and all(v is not None for v in results_ts):
        delta = results_ts[-1] - results_ts[0]
        dt = (len(results_ts) - 1) * 1.0
        fps = delta / dt if dt > 0 and delta > 0 else 0
        print(f"   → FPS: {fps:.1f} ({'✅ 有效' if fps > 0 else '⚠️ 帧数未增长'})")
    else:
        print("   → ❌ 无数据或帧数未变")

    # ─── 推荐 ───
    print("\n" + "=" * 60)
    print("📋 推荐的 FPS 数据源优先级:")
    print("   1. timestats (Android 12+, 对 GL/Vulkan 游戏最可靠)")
    print("   2. gfxinfo (需要包名, Android 12+)")
    print("   3. SurfaceFlinger buffer count (通用回退)")
    print("   程序会自动按此顺序探测并锁定第一个返回 >0 FPS 的源")


if __name__ == "__main__":
    main()