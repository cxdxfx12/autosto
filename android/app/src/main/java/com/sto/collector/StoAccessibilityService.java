package com.sto.collector;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.PowerManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Function;
import java.util.function.Predicate;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 无障碍服务：负责所有「动申行者」的操作（找节点、点击、粘贴、导航、收口）。
 * 所有 UI 动作都跑在自身的 HandlerThread 上（runOnUi），避免阻塞。
 */
public class StoAccessibilityService extends AccessibilityService {

    private static StoAccessibilityService sInstance;
    private Handler uiHandler;
    private static final String PKG_TIP = "请先在系统设置→无障碍开启「运单揽收」服务";

    // 通用弹窗关闭词（不含「确定」，避免误点录入页提交按钮）
    private static final String[] DIALOG_DISMISS = {"知道了", "我知道了", "好的", "关闭", "确认", "继续"};

    /** 一轮的状态（重复整批ok / 拦截件单号集）。 */
    static class State {
        boolean repeatAll = false;
        final Set<String> interceptions = new HashSet<>();
    }

    private static volatile boolean busy = false;

    @Override
    public void onCreate() {
        super.onCreate();
        sInstance = this;
        HandlerThread t = new HandlerThread("sto-ui");
        t.start();
        uiHandler = new Handler(t.getLooper());
        LogBus.log("无障碍服务已创建");
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        sInstance = this;
        // 允许取窗口内容（已在 xml 声明，这里再保险设一次）
        AccessibilityServiceInfo info = getServiceInfo();
        if (info != null) {
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
            setServiceInfo(info);
        }
        LogBus.log("无障碍服务已连接");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 被动监听不需要处理事件，主动调用 getRootInActiveWindow 即可
    }

    @Override
    public void onInterrupt() {
    }

    public static StoAccessibilityService getInstance() {
        return sInstance;
    }

    public void runOnUi(Runnable r) {
        if (uiHandler != null) uiHandler.post(r);
    }

    /* ============ 基础工具 ============ */

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }

    private static boolean textMatches(AccessibilityNodeInfo n, Pattern p) {
        CharSequence t = n.getText();
        if (t != null && p.matcher(t).find()) return true;
        CharSequence d = n.getContentDescription();
        if (d != null && p.matcher(d).find()) return true;
        return false;
    }

    private static boolean idContains(AccessibilityNodeInfo n, String s) {
        CharSequence id = n.getViewIdResourceName();
        return id != null && id.toString().contains(s);
    }

    private static boolean classContains(AccessibilityNodeInfo n, String s) {
        CharSequence c = n.getClassName();
        return c != null && c.toString().toLowerCase().contains(s);
    }

    private AccessibilityNodeInfo dfs(AccessibilityNodeInfo node, Predicate<AccessibilityNodeInfo> pred) {
        if (node == null) return null;
        if (pred.test(node)) return node;
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo r = dfs(node.getChild(i), pred);
            if (r != null) return r;
        }
        return null;
    }

    /** 在 root 下搜索并执行 func（func 在节点有效期内调用）；自动回收 root 与命中的节点。 */
    private boolean actOn(Predicate<AccessibilityNodeInfo> pred, Function<AccessibilityNodeInfo, Boolean> func) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return false;
        boolean res = false;
        try {
            AccessibilityNodeInfo n = dfs(root, pred);
            if (n != null) {
                Boolean r = func.apply(n);
                res = Boolean.TRUE.equals(r);
                n.recycle();
            }
        } finally {
            root.recycle();
        }
        return res;
    }

    /** 点击：从节点向上找可点击祖先。 */
    private boolean clickNode(AccessibilityNodeInfo n) {
        AccessibilityNodeInfo cur = n;
        while (cur != null) {
            if (cur.isClickable()) return cur.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            cur = cur.getParent();
        }
        return false;
    }

    public boolean clickText(Pattern p) {
        return actOn(n -> textMatches(n, p), this::clickNode);
    }

    public boolean clickIdContains(String s) {
        return actOn(n -> idContains(n, s), this::clickNode);
    }

    public String readText(Pattern p) {
        final String[] holder = {null};
        actOn(n -> textMatches(n, p), n -> {
            holder[0] = n.getText() == null ? null : n.getText().toString();
            return true;
        });
        return holder[0];
    }

    public String readIdContains(String s) {
        final String[] holder = {null};
        actOn(n -> idContains(n, s), n -> {
            holder[0] = n.getText() == null ? null : n.getText().toString();
            return true;
        });
        return holder[0];
    }

    private boolean hasEditText() {
        return actOn(n -> classContains(n, "edittext"), n -> true);
    }

    private boolean currentPkgEquals(String pkg) {
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) return false;
        CharSequence p = root.getPackageName();
        root.recycle();
        return p != null && p.toString().equals(pkg);
    }

    private void launchPkg(String pkg) {
        try {
            Intent i = getPackageManager().getLaunchIntentForPackage(pkg);
            if (i != null) {
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(i);
            }
        } catch (Exception e) {
            LogBus.log("[导航] 拉起 " + pkg + " 失败：" + e);
        }
    }

    private void wake() {
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            PowerManager.WakeLock wl = pm.newWakeLock(PowerManager.FULL_WAKE_LOCK, "sto:wake");
            wl.acquire(5000);
        } catch (Exception ignored) {
        }
    }

    /* ============ 揽收流程 ============ */

    /** 进录入页：不在申行者就拉起；找批量输入框。 */
    public boolean ensureInputPage(Config cfg) {
        if (!currentPkgEquals(cfg.pkg)) {
            launchPkg(cfg.pkg);
            sleep(3000);
        }
        if (hasEditText()) return true;
        clickText(Pattern.compile("批量录入|揽件扫描|揽件任务|批量"));
        sleep(2000);
        return hasEditText();
    }

    /** 写剪贴板 → 长按输入框粘贴 → 点录入页「确定」。 */
    public boolean pasteAndSubmit(String text, State st) {
        ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        cm.setPrimaryClip(ClipData.newPlainText("sto", text));
        sleep(400);
        boolean ok = actOn(n -> classContains(n, "edittext"), n -> {
            n.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK);
            sleep(800);
            if (clickText(Pattern.compile("粘贴"))) return true;
            android.os.Bundle b = new android.os.Bundle();
            b.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            return n.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, b);
        });
        if (!ok) {
            LogBus.log("[粘贴] 找不到批量输入框");
            return false;
        }
        sleep(1000);
        handleDialogs(st); // 粘贴后可能存在的提示
        if (!clickText(Pattern.compile("^确定$"))) {
            LogBus.log("[提交] 找不到录入页确定按钮");
            return false;
        }
        // 等列表生成（出现「完成」或上传按钮）
        boolean gen = actOn(n -> textMatches(n, Pattern.compile("完成")) || idContains(n, "tvUpload"), n -> true);
        if (!gen) {
            LogBus.log("[提交] 点确定后未进入列表/扫描页");
            return false;
        }
        return true;
    }

    private int readUploadCount() {
        String t = readIdContains("tvUpload");
        if (t == null) t = readText(Pattern.compile("^上传"));
        if (t == null) return -1;
        Matcher m = Pattern.compile("\\((\\d+)\\)").matcher(t);
        if (m.find()) return Integer.parseInt(m.group(1));
        return -1; // 无括号：可能是「上传」无计数，视为存在
    }

    /** 点完成进扫描页 → 点上传(N) → 等计数归零收口。 */
    public void uploadAndCollect(State st) {
        if (!clickText(Pattern.compile("^完成$"))) {
            LogBus.log("[收口] 没找到完成按钮，可能已在扫描页");
        }
        sleep(1500);

        long deadline = System.currentTimeMillis() + 180000;
        while (System.currentTimeMillis() < deadline) {
            handleDialogs(st); // 收口阶段也可能弹拦截/重复
            int n = readUploadCount();
            if (n == 0) {
                LogBus.log("[收口] 计数归零，上传完成");
                return;
            }
            if (n > 0) LogBus.log("[收口] 待上传 " + n);
            // 若上传按钮消失了且回到首页/安全页，视为收口
            if (readText(Pattern.compile("派件任务|揽件任务|请扫描运单条码")) != null) {
                LogBus.log("[收口] 回到首页，视为收口");
                return;
            }
            sleep(1200);
        }
        LogBus.log("[收口] 超时，强制结束本轮");
    }

    /** 弹窗处理：返回是否处理了弹窗。 */
    private boolean handleDialogs(State st) {
        // (A) 重复单号：「有N个重复单号,已经过滤」= 整批已揽收
        if (readText(Pattern.compile("重复单号|已经过滤")) != null) {
            st.repeatAll = true;
            LogBus.log("[弹窗] 重复单号 -> 整批按已揽收");
            clickText(Pattern.compile("我知道了|确定|好的"));
            return true;
        }
        // (B) 拦截件：提取对话框里的单号 -> fail(final)，点「移除并继续」
        String intercept = readText(Pattern.compile("拦截件?"));
        if (intercept != null) {
            Matcher m = Pattern.compile("\\d{8,}").matcher(intercept);
            while (m.find()) st.interceptions.add(m.group());
            LogBus.log("[弹窗] 拦截件 " + st.interceptions.size() + " 条 -> 移除并继续");
            clickText(Pattern.compile("移除并继续|移除"));
            return true;
        }
        // (C) 通用好词弹窗
        for (String w : DIALOG_DISMISS) {
            if (clickText(Pattern.compile("^" + Pattern.quote(w) + "$"))) return true;
        }
        return false;
    }

    /* ============ 一轮闭环（入口） ============ */

    public static void runRound(android.content.Context ctx) {
        if (busy) return;
        StoAccessibilityService svc = getInstance();
        if (svc == null) {
            LogBus.log(PKG_TIP);
            return;
        }
        busy = true;
        try {
            Config cfg = Config.load(ctx);
            JSONObject batch = Http.getJson(cfg.pcBase + "/api/phone/next?k=" + Http.enc(cfg.token));
            if (batch == null || batch.optString("batch_id", "").isEmpty()
                    || batch.optInt("count", 0) <= 0) {
                LogBus.log("[轮询] 无待揽收单");
                return;
            }
            Http.heartbeat(cfg);
            Http.peerYield(cfg, 180); // 碰手机前让路
            State st = new State();
            String batchId = batch.optString("batch_id");
            String text = batch.optString("text", "");
            String[] all = text.split(",");
            try {
                svc.wake();
                if (!svc.ensureInputPage(cfg)) {
                    LogBus.log("[轮询] 进录入页失败");
                    return;
                }
                if (!svc.pasteAndSubmit(text, st)) {
                    LogBus.log("[轮询] 粘贴提交失败");
                    return;
                }
                svc.uploadAndCollect(st);

                // 结算口径
                List<String> ok = new ArrayList<>();
                List<JSONObject> fail = new ArrayList<>();
                if (st.repeatAll) {
                    for (String w : all) ok.add(w);
                } else {
                    Set<String> bad = st.interceptions;
                    for (String w : all) if (!bad.contains(w)) ok.add(w);
                    for (String w : bad) {
                        JSONObject o = new JSONObject();
                        o.put("waybill", w);
                        o.put("reason", "拦截");
                        o.put("final", true);
                        fail.add(o);
                    }
                }
                Http.report(cfg, ctx, batchId, ok, fail);
            } catch (Exception e) {
                LogBus.log("[轮次异常] " + e + "（上传可能已部分成功，尽力回写）");
                // 兜底：把发出去的单全记 ok，避免滞留 pending
                List<String> ok = new ArrayList<>();
                for (String w : all) if (!w.isEmpty()) ok.add(w);
                Http.report(cfg, ctx, batchId, ok, new ArrayList<>());
            } finally {
                Http.peerYield(cfg, 0); // 释放让路
            }
        } catch (Exception e) {
            LogBus.log("[runRound] " + e);
        } finally {
            busy = false;
        }
    }
}
