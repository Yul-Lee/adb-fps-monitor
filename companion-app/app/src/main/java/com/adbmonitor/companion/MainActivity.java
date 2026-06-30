package com.adbmonitor.companion;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.TextView;

import com.adbmonitor.companion.server.MonitorHttpServer;

/**
 * 极简 UI — 显示状态、目标包名、端口，启动/停止 Service
 * 使用静态标志位 + 定时器刷新 UI
 */
public class MainActivity extends Activity {

    private TextView tvStatus;
    private TextView tvTarget;
    private TextView tvPort;
    private Button btnToggle;
    private String targetPackage;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private final Runnable statusChecker = new Runnable() {
        @Override
        public void run() {
            updateUI();
            handler.postDelayed(this, 500);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        tvStatus = findViewById(R.id.tv_status);
        tvTarget = findViewById(R.id.tv_target);
        tvPort = findViewById(R.id.tv_port);
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
            // 自动启动 Service
            if (!MonitorService.isRunning()) {
                startMonitorService();
            }
            // 关闭 Activity（前台 Service 会保活，不受 MIUI 冻结影响）
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
        } else {
            tvStatus.setText("○ 已停止");
            tvStatus.setTextColor(0xFFF38BA8);
            btnToggle.setText("启动");
        }
    }
}
