package com.adbmonitor.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Log;

import com.adbmonitor.companion.monitor.MemoryMonitor;
import com.adbmonitor.companion.monitor.NetworkMonitor;
import com.adbmonitor.companion.monitor.PowerMonitor;
import com.adbmonitor.companion.server.MonitorHttpServer;

/**
 * 前台 Service — 持有 HTTP Server 和各 Monitor
 * 前台 Service 不会被 MIUI 等系统回收
 */
public class MonitorService extends Service {

    private static final String TAG = "MonitorService";
    private static final String CHANNEL_ID = "adbmonitor_channel";
    private static final int NOTIFICATION_ID = 1;

    private MonitorHttpServer httpServer;
    private PowerManager.WakeLock wakeLock;
    private String targetPackage;

    private static volatile boolean sRunning = false;

    public static boolean isRunning() {
        return sRunning;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "adbmonitor:monitor");
        wakeLock.acquire();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null) {
            targetPackage = intent.getStringExtra("target_package");
        }

        // 启动前台（通知栏常驻，防止系统回收）
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("ADB Monitor")
                .setContentText("监控中: " + (targetPackage != null ? targetPackage : "未知"))
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setOngoing(true)
                .build();
        startForeground(NOTIFICATION_ID, notification);

        if (httpServer == null) {
            try {
                PowerMonitor power = new PowerMonitor(this);
                MemoryMonitor memory = new MemoryMonitor(this);
                NetworkMonitor network = new NetworkMonitor(this);

                if (targetPackage != null) {
                    memory.setTargetPackage(targetPackage);
                    network.setTargetPackage(targetPackage);
                }

                httpServer = new MonitorHttpServer(power, memory, network);
                httpServer.start();
                sRunning = true;
                Log.i(TAG, "HTTP server started on port " + MonitorHttpServer.PORT
                        + ", target=" + targetPackage);
            } catch (Exception e) {
                Log.e(TAG, "Failed to start HTTP server", e);
                sRunning = false;
                stopForeground(true);
                stopSelf();
            }
        }

        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (httpServer != null) {
            httpServer.stop();
            httpServer = null;
        }
        if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
        sRunning = false;
        stopForeground(true);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createNotificationChannel() {
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "ADB Monitor", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("ADB Monitor 后台监控服务");
        channel.setShowBadge(false);
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(channel);
    }
}
