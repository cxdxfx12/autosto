package com.sto.collector;

import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.Editable;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import java.util.concurrent.atomic.AtomicInteger;

/** 配置与启停界面。 */
public class MainActivity extends android.app.Activity implements LogBus.Listener {

    private EditText etPcBase, etToken, etPeerHost, etPeerToken;
    private TextView tvLog;
    private final AtomicInteger logLines = new AtomicInteger(0);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        etPcBase = findViewById(R.id.et_pc_base);
        etToken = findViewById(R.id.et_token);
        etPeerHost = findViewById(R.id.et_peer_host);
        etPeerToken = findViewById(R.id.et_peer_token);
        tvLog = findViewById(R.id.tv_log);

        Config cfg = Config.load(this);
        etPcBase.setText(cfg.pcBase);
        etToken.setText(cfg.token);
        etPeerHost.setText(cfg.peerHost);
        etPeerToken.setText(cfg.peerToken);

        Button btnAcc = findViewById(R.id.btn_acc);
        Button btnStart = findViewById(R.id.btn_start);
        Button btnStop = findViewById(R.id.btn_stop);

        btnAcc.setOnClickListener(v -> {
            try {
                startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
            } catch (Exception e) {
                LogBus.log("打开无障碍设置失败：" + e);
            }
        });

        btnStart.setOnClickListener(v -> {
            Config c = new Config();
            c.pcBase = str(etPcBase);
            c.token = str(etToken);
            c.peerHost = str(etPeerHost);
            c.peerToken = str(etPeerToken);
            Config.save(this, c);
            Intent it = new Intent(this, CollectorService.class);
            if (Build.VERSION.SDK_INT >= 26) startForegroundService(it);
            else startForeground(it);
            LogBus.log("已下发开始指令");
        });

        btnStop.setOnClickListener(v -> {
            stopService(new Intent(this, CollectorService.class));
            LogBus.log("已下发停止指令");
        });

        LogBus.register(this);
        LogBus.log("运单揽收已就绪。请先开启无障碍服务，再点「开始揽收」。");
    }

    private void startForeground(Intent it) {
        startService(it);
    }

    private static String str(EditText et) {
        Editable e = et.getText();
        return e == null ? "" : e.toString().trim();
    }

    @Override
    public void onLog(String line) {
        runOnUiThread(() -> {
            String cur = tvLog.getText() == null ? "" : tvLog.getText().toString();
            cur = line + "\n" + cur;
            // 只保留最近 200 行，避免无限增长
            if (logLines.incrementAndGet() > 200) {
                int idx = cur.lastIndexOf('\n');
                if (idx > 0) cur = cur.substring(0, idx);
                logLines.set(150);
            }
            tvLog.setText(cur);
        });
    }

    @Override
    protected void onDestroy() {
        LogBus.unregister(this);
        super.onDestroy();
    }
}
