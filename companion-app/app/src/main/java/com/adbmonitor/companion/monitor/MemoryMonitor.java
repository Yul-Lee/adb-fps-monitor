package com.adbmonitor.companion.monitor;

import android.app.ActivityManager;
import android.content.Context;
import android.os.Debug;

/**
 * 通过 ActivityManager API 读取内存数据
 * 比 ADB dumpsys meminfo（~3s 延迟）更快
 */
public class MemoryMonitor {

    private final ActivityManager activityManager;
    private String targetPackage;
    private int targetPid = -1;

    public MemoryMonitor(Context context) {
        activityManager = (ActivityManager) context.getSystemService(Context.ACTIVITY_SERVICE);
    }

    public void setTargetPackage(String pkg) {
        this.targetPackage = pkg;
        this.targetPid = -1;
    }

    private int findPid() {
        if (targetPid > 0) return targetPid;
        if (targetPackage == null) return -1;
        for (ActivityManager.RunningAppProcessInfo info : activityManager.getRunningAppProcesses()) {
            if (info.processName.equals(targetPackage)) {
                targetPid = info.pid;
                return targetPid;
            }
        }
        return -1;
    }

    /**
     * @return JSON 片段
     */
    public String read() {
        // 系统内存
        ActivityManager.MemoryInfo memInfo = new ActivityManager.MemoryInfo();
        activityManager.getMemoryInfo(memInfo);
        long totalMb = memInfo.totalMem / (1024 * 1024);
        long availMb = memInfo.availMem / (1024 * 1024);

        // 目标 app PSS
        long targetPssKb = 0;
        int pid = findPid();
        if (pid > 0) {
            int[] pids = {pid};
            Debug.MemoryInfo[] memInfos = activityManager.getProcessMemoryInfo(pids);
            if (memInfos != null && memInfos.length > 0) {
                targetPssKb = memInfos[0].getTotalPss();
            }
        }

        return String.format(java.util.Locale.US,
                "\"total_mb\":%d,\"avail_mb\":%d,\"target_pss_mb\":%d",
                totalMb, availMb, targetPssKb / 1024);
    }
}
