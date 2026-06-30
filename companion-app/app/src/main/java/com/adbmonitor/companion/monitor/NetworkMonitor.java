package com.adbmonitor.companion.monitor;

import android.content.Context;
import android.content.pm.PackageManager;
import android.net.TrafficStats;
import android.util.Log;

/**
 * 通过 TrafficStats API 读取网络流量
 * per-app 不可用时回退到系统级
 */
public class NetworkMonitor {

    private static final String TAG = "NetworkMonitor";

    private final PackageManager packageManager;
    private int targetUid = -1;
    private boolean useSystemWide = false;
    private long lastRxBytes = 0;
    private long lastTxBytes = 0;
    private long lastTimeMs = 0;

    public NetworkMonitor(Context context) {
        packageManager = context.getPackageManager();
    }

    public void setTargetPackage(String pkg) {
        try {
            targetUid = packageManager.getPackageUid(pkg, 0);
            Log.i(TAG, "Target UID: " + targetUid + " for " + pkg);
        } catch (PackageManager.NameNotFoundException e) {
            targetUid = -1;
            Log.w(TAG, "Package not found: " + pkg);
        }
        useSystemWide = false;
        lastRxBytes = 0;
        lastTxBytes = 0;
        lastTimeMs = 0;
    }

    /**
     * @return JSON 片段
     */
    public String read() {
        long rxBytes, txBytes;

        if (targetUid >= 0 && !useSystemWide) {
            rxBytes = TrafficStats.getUidRxBytes(targetUid);
            txBytes = TrafficStats.getUidTxBytes(targetUid);
            if (rxBytes < 0 || txBytes < 0) {
                // per-app 不可用，回退到系统级，立即建立基准
                Log.i(TAG, "Per-app stats unavailable, falling back to system-wide");
                useSystemWide = true;
                rxBytes = TrafficStats.getTotalRxBytes();
                txBytes = TrafficStats.getTotalTxBytes();
                lastRxBytes = rxBytes;
                lastTxBytes = txBytes;
                lastTimeMs = System.currentTimeMillis();
            } else {
                Log.d(TAG, "UID " + targetUid + " raw: rx=" + rxBytes + " tx=" + txBytes);
            }
        } else {
            rxBytes = TrafficStats.getTotalRxBytes();
            txBytes = TrafficStats.getTotalTxBytes();
        }

        if (rxBytes < 0) rxBytes = 0;
        if (txBytes < 0) txBytes = 0;

        long rxRate = 0, txRate = 0;
        long now = System.currentTimeMillis();
        if (lastTimeMs > 0) {
            long dtMs = now - lastTimeMs;
            if (dtMs > 0) {
                rxRate = (rxBytes - lastRxBytes) * 1000 / dtMs / 1024; // KB/s
                txRate = (txBytes - lastTxBytes) * 1000 / dtMs / 1024;
                if (rxRate < 0) rxRate = 0;
                if (txRate < 0) txRate = 0;
            }
        }
        lastRxBytes = rxBytes;
        lastTxBytes = txBytes;
        lastTimeMs = now;

        return String.format(java.util.Locale.US,
                "\"rx_bytes\":%d,\"tx_bytes\":%d,\"rx_rate_kbps\":%d,\"tx_rate_kbps\":%d",
                rxBytes, txBytes, rxRate, txRate);
    }
}
