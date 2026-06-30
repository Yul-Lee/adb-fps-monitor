package com.adbmonitor.companion.server;

import com.adbmonitor.companion.monitor.PowerMonitor;
import com.adbmonitor.companion.monitor.MemoryMonitor;
import com.adbmonitor.companion.monitor.NetworkMonitor;

import java.util.Map;

/**
 * HTTP 服务器 — 路由 /api/data、/api/ping
 */
public class MonitorHttpServer {

    public static final int PORT = 18765;

    private final NanoHTTPD server;
    private final PowerMonitor powerMonitor;
    private final MemoryMonitor memoryMonitor;
    private final NetworkMonitor networkMonitor;

    public MonitorHttpServer(PowerMonitor power, MemoryMonitor memory, NetworkMonitor network) {
        this.powerMonitor = power;
        this.memoryMonitor = memory;
        this.networkMonitor = network;

        server = new NanoHTTPD(PORT);
        server.addRoute("/api/ping", this::handlePing);
        server.addRoute("/api/data", this::handleData);
    }

    public void start() throws java.io.IOException {
        server.start();
    }

    public void stop() {
        server.stop();
    }

    private NanoHTTPD.Response handlePing(String uri, Map<String, String> params) {
        return NanoHTTPD.Response.ok("{\"status\":\"ok\"}");
    }

    private NanoHTTPD.Response handleData(String uri, Map<String, String> params) {
        String kind = params.get("kind");
        StringBuilder sb = new StringBuilder("{");

        if (kind == null || kind.equals("power")) {
            sb.append("\"power\":{").append(powerMonitor.read()).append("}");
        }
        if (kind == null || kind.equals("memory")) {
            if (sb.length() > 1) sb.append(",");
            sb.append("\"memory\":{").append(memoryMonitor.read()).append("}");
        }
        if (kind == null || kind.equals("network")) {
            if (sb.length() > 1) sb.append(",");
            sb.append("\"network\":{").append(networkMonitor.read()).append("}");
        }

        sb.append("}");
        return NanoHTTPD.Response.ok(sb.toString());
    }
}
