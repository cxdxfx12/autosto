package com.sto.collector;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;

/** 前台服务：常驻轮询电脑接口，收到单就交给无障碍服务去动申行者。 */
public class CollectorService extends Service {

    private static final int NOTIF_ID = 1;
    private static final String CHANNEL = "sto_foreground";
    private static final long POLL_MS = 30000;

    private HandlerThread pollThread;
    private Handler pollHandler;
    private Runnable tick;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        startForegroundSafe();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (pollThread == null) {
            pollThread = new HandlerThread("sto-poll");
            pollThread.start();
            pollHandler = new Handler(pollThread.getLooper());
            tick = () -> {
                doRound();
                if (pollHandler != null) pollHandler.postDelayed(tick, POLL_MS);
            };
            pollHandler.post(tick);
            LogBus.log("揽收轮询已启动");
        }
        return START_STICKY;
    }

    private void doRound() {
        StoAccessibilityService svc = StoAccessibilityService.getInstance();
        if (svc == null) {
            LogBus.log("无障碍服务未开启，跳过本轮（请先到系统设置开启）");
            return;
        }
        svc.runOnUi(() -> StoAccessibilityService.runRound(getApplicationContext()));
    }

    private void startForegroundSafe() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "运单揽收前台", NotificationManager.IMPORTANCE_LOW);
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
        Notification n = new Notification.Builder(this, CHANNEL)
                .setContentTitle("运单揽收运行中")
                .setContentText("正在轮询电脑接口")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .build();
        startForeground(NOTIF_ID, n);
    }

    @Override
    public void onDestroy() {
        if (pollHandler != null && tick != null) pollHandler.removeCallbacks(tick);
        if (pollThread != null) pollThread.quitSafely();
        stopForeground(true);
        LogBus.log("揽收服务已停止");
        super.onDestroy();
    }
}
