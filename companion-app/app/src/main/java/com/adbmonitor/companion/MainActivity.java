package com.adbmonitor.companion;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import com.adbmonitor.companion.server.MonitorHttpServer;

import java.util.Locale;

/**
 * 极简 UI — 显示状态、目标包名、端口、实时数据，启动/停止 Service
 */
public class MainActivity extends Activity {

    private TextView tvStatus;
    private TextView tvTarget;
    private TextView tvPort;
    private TextView tvPower;
    private TextView tvBattery;
    private TextView tvMemory;
    private TextView tvNetwork;
    private LinearLayout dataPanel;
    private Button btnToggle;
    private String targetPackage;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private final Runnable statusChecker = new Runnable() {
        @Override
        public void run() {
            updateUI();
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        tvStatus = findViewById(R.id.tv_status);
        tvTarget = findViewById(R.id.tv_target);
        tvPort = findViewById(R.id.tv_port);
        tvPower = findViewById(R.id.tv_power);
        tvBattery = findViewById(R.id.tv_battery);
        tvMemory = findViewById(R.id.tv_memory);
        tvNetwork = findViewById(R.id.tv_network);
        dataPanel = findViewById(R.id.data_panel);
        btnToggle = findViewById(R.id.btn_toggle);

        tvPort.setText("端口: " + MonitorHttpServer.PORT);

        btnToggle.setOnClickListener(v -> {
            if (MonitorService.isRunning()) {
                stopService(new Intent(this, MonitorService.class));
            } else {
                startMonitorService();
            }
        });

        handleIntent(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
    }

    private void handleIntent(Intent intent) {
        String pkg = intent.getStringExtra("target_package");
        if (pkg != null) {
            targetPackage = pkg;
            tvTarget.setText("目标: " + pkg);
            if (!MonitorService.isRunning()) {
                startMonitorService();
            }
            handler.postDelayed(() -> finish(), 500);
        }
    }

    private void startMonitorService() {
        Intent svc = new Intent(this, MonitorService.class);
        if (targetPackage != null) {
            svc.putExtra("target_package", targetPackage);
        }
        startService(svc);
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(statusChecker);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(statusChecker);
    }

    private void updateUI() {
        boolean running = MonitorService.isRunning();
        if (running) {
            tvStatus.setText("● 运行中");
            tvStatus.setTextColor(0xFFA6E3A1);
            btnToggle.setText("停止");
            dataPanel.setVisibility(View.VISIBLE);
            updateData();
        } else {
            tvStatus.setText("○ 已停止");
            tvStatus.setTextColor(0xFFF38BA8);
            btnToggle.setText("启动");
            dataPanel.setVisibility(View.GONE);
        }
    }

    private void updateData() {
        // 功耗
        String powerJson = MonitorService.readPowerJson();
        double currentMa = parseDouble(powerJson, "current_mA");
        double voltageV = parseDouble(powerJson, "voltage_V");
        double powerMw = parseDouble(powerJson, "power_mw");
        if (currentMa > 0 || powerMw > 0) {
            tvPower.setText(String.format(Locale.US, "功耗: %.0fmA / %.2fV / %.0fmW", currentMa, voltageV, powerMw));
        } else {
            tvPower.setText("功耗: 电压 " + String.format(Locale.US, "%.2fV", voltageV));
        }

        // 电量
        int capacity = parseInt(powerJson, "capacity");
        tvBattery.setText(String.format(Locale.US, "电量: %d%%", capacity));

        // 内存
        String memJson = MonitorService.readMemoryJson();
        long totalMb = parseLong(memJson, "total_mb");
        long availMb = parseLong(memJson, "avail_mb");
        if (totalMb > 0) {
            tvMemory.setText(String.format(Locale.US, "内存: 可用 %dMB / 总 %dMB", availMb, totalMb));
        }

        // 网络
        String netJson = MonitorService.readNetworkJson();
        long rxRate = parseLong(netJson, "rx_rate_kbps");
        long txRate = parseLong(netJson, "tx_rate_kbps");
        tvNetwork.setText(String.format(Locale.US, "网络: ↓%dKB/s ↑%dKB/s", rxRate, txRate));
    }

    private double parseDouble(String json, String key) {
        String search = "\"" + key + "\":";
        int idx = json.indexOf(search);
        if (idx < 0) return 0;
        int start = idx + search.length();
        int end = start;
        while (end < json.length() && (Character.isDigit(json.charAt(end)) || json.charAt(end) == '.' || json.charAt(end) == '-')) {
            end++;
        }
        try {
            return Double.parseDouble(json.substring(start, end));
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private int parseInt(String json, String key) {
        return (int) parseDouble(json, key);
    }

    private long parseLong(String json, String key) {
        return (long) parseDouble(json, key);
    }
}
