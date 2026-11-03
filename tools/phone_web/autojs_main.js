// 运单揽件助手 · 手机端自动化脚本（AutoJs6）
// 作用：轮询电脑上的批次 → 把单号灌进申行者「揽件扫描」输入框 → 点确定 → 回传结果
// 用法：在 AutoJs6 里新建脚本，粘贴本文件全部内容，直接运行。
// 前提：手机停在「申行者 → 揽件扫描」页；AutoJs6 已开启无障碍服务。

var BASE = "http://192.168.1.209:8766";
var KEY = "PFHIyVjK9V5GgeFQ00aV2xK4v-Ndsiv4";
var PKG = "cn.sto.sxz.core.app";
var POLL_MS = 3000;

var CONFIRM_WORDS = ["确定", "确认", "开始揽收", "提交", "立即揽收"];
var SUCCESS_HINTS = ["揽收成功", "扫描成功", "操作成功", "成功"];
var IDLE_TIP = "运单助手：等待新批次…";

// ---------- 基础工具 ----------

function apiGet(path) {
    try {
        var r = http.get(BASE + path, { timeout: 8000 });
        var body = r.body ? (r.body.string ? r.body.string() : String(r.body)) : String(r);
        return JSON.parse(body);
    } catch (e) {
        return null;
    }
}

function apiPost(path, obj) {
    try {
        var r = http.postJson(BASE + path, obj, { timeout: 15000 });
        var body = r.body ? (r.body.string ? r.body.string() : String(r.body)) : String(r);
        return JSON.parse(body);
    } catch (e) {
        toast("回传失败：" + e);
        return null;
    }
}

function allTexts() {
    // 把当前屏幕上所有可见文字捞出来，用于结果判定与排错
    var out = [];
    try {
        var nodes = textMatches(/.*/).find();
        for (var i = 0; i < nodes.length; i++) {
            var t = nodes[i].text();
            if (t) out.push(String(t));
        }
    } catch (e) {
    }
    return out;
}

function findInput() {
    var box = className("android.widget.EditText").findOnce();
    if (box) return box;
    var list = className("android.widget.EditText").find();
    return list && list.size() > 0 ? list.get(0) : null;
}

function findConfirm() {
    for (var i = 0; i < CONFIRM_WORDS.length; i++) {
        var w = CONFIRM_WORDS[i];
        var n = text(w).findOnce();
        if (n) return n;
    }
    for (var j = 0; j < CONFIRM_WORDS.length; j++) {
        var n2 = textContains(CONFIRM_WORDS[j]).findOnce();
        if (n2) return n2;
    }
    return null;
}

function clickNode(n) {
    var b = n.bounds();
    click(b.centerX(), b.centerY());
}

function waitFor(fn, ms, step) {
    var end = Date.now() + ms;
    while (Date.now() < end) {
        var v = fn();
        if (v) return v;
        sleep(step || 500);
    }
    return null;
}

// ---------- 主流程 ----------

function uploadDump(tag) {
    try {
        var parts = [];
        var nodes = textMatches(/.*/).find();
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            var b = n.bounds();
            parts.push(n.className() + "|" + (n.id() || "") + "|" + (n.text() || "") +
                "|clickable=" + n.clickable() + "|[" + b.left + "," + b.top + "," + b.right + "," + b.bottom + "]");
        }
        apiPost("/api/phone/dump?k=" + KEY, { tag: tag, dump: parts.join("\n") });
    } catch (e) {
    }
}

function runOnce() {
    var d = apiGet("/api/phone/next?k=" + KEY);
    if (!d) {
        toast("连不上电脑 " + BASE);
        return false;
    }
    if (!d.count) return false;

    toast("收到 " + d.count + " 条单号");
    setClip(d.text);

    // 确保申行者在最前；本脚本不负责导航，只处理"揽件扫描"页
    app.launchPackage(PKG);
    sleep(1200);

    var box = waitFor(findInput, 8000, 400);
    if (!box) {
        toast("没找到输入框：请先切到「揽件扫描」页");
        uploadDump("no-input");
        return false;
    }

    box.setText(d.text);
    sleep(600);

    var btn = findConfirm();
    if (!btn) {
        toast("没找到「确定」按钮");
        uploadDump("no-confirm");
        return false;
    }
    clickNode(btn);

    // 等结果：出现成功/失败字样，或者输入框被清空
    var texts = waitFor(function () {
        var t = allTexts().join(" ");
        if (t.indexOf("揽收成功") >= 0 || t.indexOf("扫描成功") >= 0 || t.indexOf("失败") >= 0) return t;
        return null;
    }, 120000, 1000);

    uploadDump("result");
    var joined = texts || allTexts().join(" ");
    var failed = [];
    var re = /\b\d{15}\b/g;
    var m;
    while ((m = re.exec(joined)) !== null) {
        if (d.text.indexOf(m[0]) >= 0) failed.push(m[0]);
    }

    var ok = false;
    for (var i = 0; i < SUCCESS_HINTS.length; i++) {
        if (joined.indexOf(SUCCESS_HINTS[i]) >= 0) {
            ok = true;
            break;
        }
    }

    apiPost("/api/phone/report?k=" + KEY, {
        batch_id: d.batch_id,
        fail: failed,
        finished: ok,
        note: ok ? ("手机脚本完成，失败 " + failed.length + " 条") : "未识别到成功提示"
    });
    toast(ok ? ("已回传：失败 " + failed.length + " 条") : "结果没认出来，已把界面传回电脑");
    return true;
}

// ---------- 启动 ----------

if (!device.isScreenOn()) {
    device.wakeUp();
    sleep(800);
}
toast(IDLE_TIP);
while (true) {
    try {
        runOnce();
    } catch (e) {
        toast("脚本出错：" + e);
        sleep(3000);
    }
    sleep(POLL_MS);
}
