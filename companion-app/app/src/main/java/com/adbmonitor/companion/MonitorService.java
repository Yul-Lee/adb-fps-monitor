package com.adbmonitor.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;

import java.util.Locale;

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
    private NotificationManager notificationManager;
    private final Handler notifyHandler = new Handler(Looper.getMainLooper());

    private final Runnable notifyUpdater = new Runnable() {
        @Override
        public void run() {
            if (sRunning) {
                updateNotification();
                notifyHandler.postDelayed(this, 5000);
            }
        }
    };

    private static volatile boolean sRunning = false;
    private static PowerMonitor sPowerMonitor;
    private static MemoryMonitor sMemoryMonitor;
    private static NetworkMonitor sNetworkMonitor;

    public static boolean isRunning() {
        return sRunning;
    }

    public static String readPowerJson() {
        PowerMonitor p = sPowerMonitor;
        return p != null ? p.read() : "";
    }

    public static String readMemoryJson() {
        MemoryMonitor m = sMemoryMonitor;
        return m != null ? m.read() : "";
    }

    public static String readNetworkJson() {
        NetworkMonitor n = sNetworkMonitor;
        return n != null ? n.read() : "";
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
        notificationManager = getSystemService(NotificationManager.class);
        Notification notification = buildNotification("监控中: " + (targetPackage != null ? targetPackage : "未知"));
        startForeground(NOTIFICATION_ID, notification);
        notifyHandler.postDelayed(notifyUpdater, 5000);

        if (httpServer == null) {
            try {
                PowerMonitor power = new PowerMonitor(this);
                MemoryMonitor memory = new MemoryMonitor(this);
                NetworkMonitor network = new NetworkMonitor(this);

                if (targetPackage != null) {
                    memory.setTargetPackage(targetPackage);
                    network.setTargetPackage(targetPackage);
                }

                sPowerMonitor = power;
                sMemoryMonitor = memory;
                sNetworkMonitor = network;

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
        sPowerMonitor = null;
        sMemoryMonitor = null;
        sNetworkMonitor = null;
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

    private Notification buildNotification(String text) {
        return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("ADB Monitor")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setOngoing(true)
                .build();
    }

    private void updateNotification() {
        if (notificationManager == null || sPowerMonitor == null) return;
        try {
            String powerJson = sPowerMonitor.read();
            double currentMa = 0;
            int capacity = 0;
            int idx1 = powerJson.indexOf("\"current_mA\":");
            if (idx1 >= 0) {
                int s = idx1 + 13;
                int e = s;
                while (e < powerJson.length() && (Character.isDigit(powerJson.charAt(e)) || powerJson.charAt(e) == '.')) e++;
                if (e > s) currentMa = Double.parseDouble(powerJson.substring(s, e));
            }
            int idx2 = powerJson.indexOf("\"capacity\":");
            if (idx2 >= 0) {
                int s = idx2 + 11;
                int e = s;
                while (e < powerJson.length() && Character.isDigit(powerJson.charAt(e))) e++;
                if (e > s) capacity = Integer.parseInt(powerJson.substring(s, e));
            }
            String status = String.format(Locale.US, "%dmA · %d%% · %s",
                    (int) currentMa, capacity, targetPackage != null ? targetPackage : "");
            notificationManager.notify(NOTIFICATION_ID, buildNotification(status));
        } catch (Exception ignored) {}
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
