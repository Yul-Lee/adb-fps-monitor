package com.adbmonitor.companion.server;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

/**
 * 极简嵌入式 HTTP 服务器（基于 NanoHTTPD 精简）
 * 仅支持 GET，用于 ADB 端口转发通信
 */
public class NanoHTTPD {

    public interface Handler {
        Response handle(String uri, Map<String, String> params);
    }

    public static class Response {
        public final int status;
        public final String contentType;
        public final String body;

        public Response(int status, String contentType, String body) {
            this.status = status;
            this.contentType = contentType;
            this.body = body;
        }

        public static Response ok(String json) {
            return new Response(200, "application/json; charset=utf-8", json);
        }

        public static Response error(int code, String msg) {
            return new Response(code, "application/json; charset=utf-8",
                    "{\"error\":\"" + msg + "\"}");
        }
    }

    private final int port;
    private final Map<String, Handler> routes = new ConcurrentHashMap<>();
    private ServerSocket serverSocket;
    private ExecutorService executor;
    private volatile boolean running;

    public NanoHTTPD(int port) {
        this.port = port;
    }

    public void addRoute(String path, Handler handler) {
        routes.put(path, handler);
    }

    public void start() throws IOException {
        serverSocket = new ServerSocket(port, 50, InetAddress.getByName("127.0.0.1"));
        serverSocket.setReuseAddress(true);
        executor = Executors.newFixedThreadPool(2);
        running = true;

        new Thread(() -> {
            while (running) {
                try {
                    Socket socket = serverSocket.accept();
                    executor.submit(() -> handleClient(socket));
                } catch (IOException e) {
                    if (running) e.printStackTrace();
                }
            }
        }, "NanoHTTPD-accept").start();
    }

    public void stop() {
        running = false;
        try { if (serverSocket != null) serverSocket.close(); } catch (IOException ignored) {}
        if (executor != null) executor.shutdownNow();
    }

    private void handleClient(Socket socket) {
        try {
            socket.setSoTimeout(10000);
            InputStream rawIn = socket.getInputStream();

            // 逐字节读取请求行（避免 BufferedReader 预读导致 ADB 转发丢数据）
            ByteArrayOutputStream lineBuf = new ByteArrayOutputStream();
            int prev = 0, cur;
            while ((cur = rawIn.read()) >= 0) {
                if (cur == '\n' && prev == '\r') {
                    lineBuf.write(cur);
                    break;
                }
                if (cur == '\n') {
                    break;
                }
                if (prev == '\r' && cur != '\n') {
                    lineBuf.write('\r');
                }
                if (cur != '\r') {
                    lineBuf.write(cur);
                }
                prev = cur;
            }
            String requestLine = lineBuf.toString("UTF-8").trim();
            if (requestLine.isEmpty() || !requestLine.startsWith("GET")) {
                sendResponse(socket, Response.error(405, "Method not allowed"));
                return;
            }

            // 解析 GET /path?query HTTP/1.1
            String[] parts = requestLine.split(" ");
            if (parts.length < 2) {
                sendResponse(socket, Response.error(400, "Bad request"));
                return;
            }

            String uri = parts[1];
            String path = uri;
            Map<String, String> params = new HashMap<>();

            int qIdx = uri.indexOf('?');
            if (qIdx >= 0) {
                path = uri.substring(0, qIdx);
                String query = uri.substring(qIdx + 1);
                for (String pair : query.split("&")) {
                    String[] kv = pair.split("=", 2);
                    if (kv.length == 2) {
                        params.put(URLDecoder.decode(kv[0], "UTF-8"),
                                   URLDecoder.decode(kv[1], "UTF-8"));
                    }
                }
            }

            // 跳过剩余 headers（逐字节读到空行）
            int crlfCount = 0;
            while (crlfCount < 2 && (cur = rawIn.read()) >= 0) {
                if (cur == '\n') {
                    crlfCount++;
                } else if (cur != '\r') {
                    crlfCount = 0;
                }
            }

            Handler handler = routes.get(path);
            if (handler != null) {
                Response resp = handler.handle(path, params);
                sendResponse(socket, resp);
            } else {
                sendResponse(socket, Response.error(404, "Not found"));
            }
        } catch (Exception e) {
            try { sendResponse(socket, Response.error(500, "Internal error: " + e.getMessage())); } catch (IOException ignored) {}
        } finally {
            try { socket.close(); } catch (IOException ignored) {}
        }
    }

    private void sendResponse(Socket socket, Response response) throws IOException {
        String reason;
        switch (response.status) {
            case 200: reason = "OK"; break;
            case 400: reason = "Bad Request"; break;
            case 404: reason = "Not Found"; break;
            case 405: reason = "Method Not Allowed"; break;
            default: reason = "Internal Server Error"; break;
        }
        String header = "HTTP/1.1 " + response.status + " " + reason + "\r\n" +
                "Content-Type: " + response.contentType + "\r\n" +
                "Content-Length: " + response.body.getBytes(StandardCharsets.UTF_8).length + "\r\n" +
                "Access-Control-Allow-Origin: *\r\n" +
                "Connection: close\r\n\r\n";
        OutputStream out = socket.getOutputStream();
        out.write(header.getBytes(StandardCharsets.UTF_8));
        out.write(response.body.getBytes(StandardCharsets.UTF_8));
        out.flush();
    }
}
