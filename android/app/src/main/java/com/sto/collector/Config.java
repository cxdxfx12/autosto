package com.sto.collector;

import android.content.Context;
import android.content.SharedPreferences;

/** 运行配置，存 SharedPreferences。 */
public final class Config {
    public static final String PREFS = "sto_collector";

    public String pcBase = "";        // 电脑局域网地址，如 http://192.168.1.50:8766
    public String token = "";         // 电脑 config.yaml 的 server.token
    public String pkg = "cn.sto.sxz.core.app"; // 申行者包名
    public String peerHost = "";      // 转发桥网关 IP（同电脑）
    public int peerPort = 8765;       // 转发桥网关端口
    public String peerToken = "";     // 转发桥 token（无则空串）

    public static Config load(Context ctx) {
        SharedPreferences sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        Config c = new Config();
        c.pcBase = sp.getString("pcBase", "");
        c.token = sp.getString("token", "");
        c.pkg = sp.getString("pkg", "cn.sto.sxz.core.app");
        c.peerHost = sp.getString("peerHost", "");
        c.peerPort = sp.getInt("peerPort", 8765);
        c.peerToken = sp.getString("peerToken", "");
        return c;
    }

    public static void save(Context ctx, Config c) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit()
                .putString("pcBase", c.pcBase)
                .putString("token", c.token)
                .putString("pkg", c.pkg)
                .putString("peerHost", c.peerHost)
                .putInt("peerPort", c.peerPort)
                .putString("peerToken", c.peerToken)
                .apply();
    }
}
