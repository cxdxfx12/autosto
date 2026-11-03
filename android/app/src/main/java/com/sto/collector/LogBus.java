package com.sto.collector;

import android.util.Log;

import java.util.concurrent.CopyOnWriteArrayList;

/** 极简日志总线：同时打 logcat 并推给 UI 监听（MainActivity 用来刷新日志框）。 */
public final class LogBus {
    public interface Listener {
        void onLog(String line);
    }

    private static final String TAG = "StoCollector";
    private static final CopyOnWriteArrayList<Listener> LISTENERS = new CopyOnWriteArrayList<>();

    private LogBus() {}

    public static void register(Listener l) {
        if (l != null) LISTENERS.add(l);
    }

    public static void unregister(Listener l) {
        LISTENERS.remove(l);
    }

    public static void log(String msg) {
        Log.i(TAG, msg);
        for (Listener l : LISTENERS) {
            try {
                l.onLog(msg);
            } catch (Throwable ignored) {
            }
        }
    }
}
