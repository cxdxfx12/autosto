package com.sto.collector;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/** 纯 HttpURLConnection 的 HTTP 客户端（无第三方依赖）。 */
public final class Http {

    private Http() {}

    public static String enc(String s) {
        try {
            return URLEncoder.encode(s == null ? "" : s, "UTF-8");
        } catch (Exception e) {
            return s == null ? "" : s;
        }
    }

    private static String readStream(InputStream in) throws IOException {
        if (in == null) return "";
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
        return new String(out.toByteArray(), StandardCharsets.UTF_8);
    }

    /** GET 返回 JSON；非 200 或异常返回 null。 */
    public static JSONObject getJson(String urlStr) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(urlStr).openConnection();
            c.setRequestMethod("GET");
            c.setConnectTimeout(10000);
            c.setReadTimeout(10000);
            int code = c.getResponseCode();
            if (code != 200) return null;
            return new JSONObject(readStream(c.getInputStream()));
        } catch (Exception e) {
            LogBus.log("[http] GET 失败 " + urlStr + " : " + e);
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** POST JSON，返回 HTTP 状态码（异常返回 0）。 */
    public static int postJson(String urlStr, JSONObject body, Map<String, String> headers) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(urlStr).openConnection();
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(15000);
            c.setReadTimeout(15000);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            if (headers != null) {
                for (Map.Entry<String, String> e : headers.entrySet()) {
                    c.setRequestProperty(e.getKey(), e.getValue());
                }
            }
            byte[] data = body.toString().getBytes(StandardCharsets.UTF_8);
            c.setFixedLengthStreamingMode(data.length);
            try (OutputStream o = c.getOutputStream()) {
                o.write(data);
            }
            return c.getResponseCode();
        } catch (Exception e) {
            LogBus.log("[http] POST 失败 " + urlStr + " : " + e);
            return 0;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    public static int postJson(String urlStr, JSONObject body) {
        return postJson(urlStr, body, null);
    }

    public static void heartbeat(Config cfg) {
        getJson(cfg.pcBase + "/api/phone/heartbeat?k=" + enc(cfg.token) + "&page=watch");
    }

    public static void peerYield(Config cfg, int seconds) {
        if (cfg.peerHost == null || cfg.peerHost.isEmpty()) return; // 没配转发桥就跳过
        try {
            JSONObject b = new JSONObject();
            b.put("seconds", seconds);
            java.util.HashMap<String, String> h = new java.util.HashMap<>();
            h.put("X-Bridge-Token", cfg.peerToken == null ? "" : cfg.peerToken);
            int code = postJson("http://" + cfg.peerHost + ":" + cfg.peerPort + "/watch-pause", b, h);
            LogBus.log("[让路] watch-pause " + seconds + "s -> " + code);
        } catch (Exception e) {
            LogBus.log("[让路] 失败(忽略)：" + e);
        }
    }

    /** 回写结果：3 次重试；全败落盘，切勿重发（上传已提交）。 */
    public static boolean report(Config cfg, Context ctx, String batchId,
                                 List<String> ok, List<JSONObject> fail) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("batch_id", batchId);
            payload.put("finished", true);
            payload.put("ok", new JSONArray(ok));
            payload.put("fail", new JSONArray(fail));
            payload.put("note", "APK批量揽收");
        } catch (JSONException e) {
            LogBus.log("[回写] 构造失败：" + e);
            return false;
        }
        for (int i = 0; i < 3; i++) {
            int code = postJson(cfg.pcBase + "/api/phone/report", payload);
            if (code == 200) {
                LogBus.log("[回写] OK batch=" + batchId);
                return true;
            }
            LogBus.log("[回写] 第" + (i + 1) + "次失败 code=" + code + "，2s 后重试");
            try {
                Thread.sleep(2000);
            } catch (InterruptedException ignored) {
            }
        }
        // 落盘待人工补报
        try {
            String name = "sto_report_fail_" + batchId + "_" + System.currentTimeMillis() + ".json";
            java.io.File f = new java.io.File(ctx.getFilesDir(), name);
            java.io.FileWriter w = new java.io.FileWriter(f);
            w.write(payload.toString(2));
            w.close();
            LogBus.log("[回写] 已落盘待补报：" + f.getAbsolutePath() + "（上传已成功，勿重发）");
        } catch (Exception e) {
            LogBus.log("[回写] 落盘也失败：" + e);
        }
        return false;
    }
}
