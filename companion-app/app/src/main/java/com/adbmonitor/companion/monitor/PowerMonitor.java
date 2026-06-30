package com.adbmonitor.companion.monitor;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;

/**
 * 通过 BatteryManager API 读取功耗数据
 * 比 ADB charge_counter 差值估算更准确
 */
public class PowerMonitor {

    private final BatteryManager batteryManager;
    private final Context context;

    public PowerMonitor(Context context) {
        this.context = context;
        batteryManager = (BatteryManager) context.getSystemService(Context.BATTERY_SERVICE);
    }

    /**
     * @return JSON 片段: "current_mA":..., "voltage_V":..., "capacity":..., "power_mW":...
     */
    public String read() {
        // 微安（负值=放电），Integer.MIN_VALUE 表示不支持
        int currentUa = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CURRENT_NOW);
        // 百分比
        int capacity = batteryManager.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);

        // 电压：通过 sticky broadcast 获取（毫伏）
        IntentFilter filter = new IntentFilter(Intent.ACTION_BATTERY_CHANGED);
        Intent batteryStatus = context.registerReceiver(null, filter);
        int voltageMv = 3800;
        if (batteryStatus != null) {
            voltageMv = batteryStatus.getIntExtra(BatteryManager.EXTRA_VOLTAGE, 3800);
        }
        double voltageV = voltageMv / 1000.0;

        // 电流：检查是否可用（不支持时返回 Integer.MIN_VALUE）
        double currentMa;
        double powerMw;
        if (currentUa != Integer.MIN_VALUE && currentUa != 0) {
            currentMa = Math.abs(currentUa) / 1000.0;
            powerMw = currentMa * voltageV;
        } else {
            // 不支持直接读电流，用 charge_counter 差值估算不可行（需要历史数据）
            // 返回电压和电量，功耗留空
            currentMa = 0;
            powerMw = 0;
        }

        return String.format(java.util.Locale.US,
                "\"current_mA\":%.0f,\"voltage_V\":%.2f,\"capacity\":%d,\"power_mw\":%.0f",
                currentMa, voltageV, capacity, powerMw);
    }
}
