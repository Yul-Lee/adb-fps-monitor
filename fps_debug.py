#!/usr/bin/env python3
"""
FPS Debug Tool - Diagnose FPS data source availability on device
Usage: python fps_debug.py [-s serial] [-p package]
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
    parser.add_argument("-n", "--rounds", type=int, default=3, help="Test rounds per source")
    args = parser.parse_args()

    adb = ADBRunner(serial=args.serial)

    # Check device
    devices = adb.check_device()
    if not devices:
        print("[FAIL] No ADB device detected")
        return
    print(f"[OK] Device: {devices[0] if len(devices) == 1 else ', '.join(devices)}")

    # Check foreground app
    pkg = args.package or adb.get_foreground_package()
    if pkg:
        print(f"[OK] Target app: {pkg}")
    else:
        print("[WARN] No package specified, some tests will be skipped")

    print("=" * 60)

    # ─── Test 1: SurfaceFlinger buffer count ───
    print("\n[*] [1] SurfaceFlinger buffer frame count")
    print("   How: parse state=ACQUIRED frame=N, track max N")
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
        print(f"   Round {i+1}: max_acquired_frame = {max_frame}")
        time.sleep(1.0)
    if len(results_sf) >= 2:
        delta = results_sf[-1] - results_sf[0]
        dt = (len(results_sf) - 1) * 1.0
        fps = delta / dt if dt > 0 and delta > 0 else 0
        print(f"   -> FPS: {fps:.1f} ({'[OK] valid' if fps > 0 else '[FAIL] no data'})")
    else:
        print("   -> [FAIL] No data")

    # ─── Test 2: SurfaceFlinger --latency ───
    print("\n[*] [2] SurfaceFlinger --latency")
    out, _ = adb.run_shell("dumpsys SurfaceFlinger --list")
    sv_candidates = []
    for line in out.split("\n"):
        line = line.strip()
        if pkg and pkg in line and "SurfaceView" in line:
            sv_candidates.append(line)
        elif "SurfaceView[" in line and "Background for" not in line:
            sv_candidates.append(line)
    if sv_candidates:
        print(f"   Found {len(sv_candidates)} SurfaceView(s):")
        for c in sv_candidates[:5]:
            print(f"     - {c}")
        win_match = re.search(r"(SurfaceView\[[^\]]+\](?:\(BLAST\))?(?:#\d+)?)", sv_candidates[0])
        if win_match:
            win = win_match.group(1)
            safe_win = win.replace("'", "'\\''")
            out, _ = adb.run_shell(f"dumpsys SurfaceFlinger --latency '{safe_win}'")
            lines = [l for l in out.strip().split("\n") if l.strip()]
            print(f"   latency data lines: {len(lines)}")
            if len(lines) > 1:
                print(f"   -> [OK] Has frame data (window: {win})")
            else:
                print(f"   -> [FAIL] Frame data empty (window: {win})")
    else:
        print("   -> [FAIL] No SurfaceView found")

    # ─── Test 3: gfxinfo ───
    if pkg:
        print(f"\n[*] [3] gfxinfo ({pkg})")
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
            print(f"   Round {i+1}: Total frames rendered = {total}")
            time.sleep(1.0)
        if len(results_gfx) >= 2 and all(v is not None for v in results_gfx):
            delta = results_gfx[-1] - results_gfx[0]
            dt = (len(results_gfx) - 1) * 1.0
            fps = delta / dt if dt > 0 and delta > 0 else 0
            print(f"   -> FPS: {fps:.1f} ({'[OK] valid' if fps > 0 else '[WARN] no frame increase'})")
        else:
            print("   -> [FAIL] No data")
    else:
        print("\n[*] [3] gfxinfo - skipped (no package)")

    # ─── Test 4: timestats ───
    print("\n[*] [4] SurfaceFlinger --timestats")
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
                print(f"   Round {i+1}: {name[-50:]}  frames={f}  avgFPS={fps}")
            results_ts.append(max(v[0] for v in layers.values()))
        else:
            print(f"   Round {i+1}: No matching SurfaceView layer")
            results_ts.append(None)
        time.sleep(1.0)

    if len(results_ts) >= 2 and all(v is not None for v in results_ts):
        delta = results_ts[-1] - results_ts[0]
        dt = (len(results_ts) - 1) * 1.0
        fps = delta / dt if dt > 0 and delta > 0 else 0
        print(f"   -> FPS: {fps:.1f} ({'[OK] valid' if fps > 0 else '[WARN] no frame increase'})")
    else:
        print("   -> [FAIL] No data or frames unchanged")

    # ─── Recommendation ───
    print("\n" + "=" * 60)
    print("[*] Recommended FPS source priority:")
    print("   1. timestats (Android 12+, most reliable for GL/Vulkan games)")
    print("   2. gfxinfo (requires package name, Android 12+)")
    print("   3. SurfaceFlinger buffer count (universal fallback)")
    print("   The tool auto-probes in this order and locks the first source with FPS > 0")


if __name__ == "__main__":
    main()
