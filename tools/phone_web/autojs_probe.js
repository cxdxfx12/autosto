// 探针脚本：把当前界面的控件树传到电脑，用来给正式脚本配选择器。
// 用法：手机上打开「申行者 → 揽件扫描」页，运行本脚本，看到「已上传」即可。

var BASE = "http://192.168.1.209:8766";
var KEY = "PFHIyVjK9V5GgeFQ00aV2xK4v-Ndsiv4";

var parts = [];
var nodes = textMatches(/.*/).find();
for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    var b = n.bounds();
    parts.push(
        n.className() + "|id=" + (n.id() || "") + "|text=" + (n.text() || "") +
        "|desc=" + (n.desc() || "") + "|clickable=" + n.clickable() +
        "|[" + b.left + "," + b.top + "," + b.right + "," + b.bottom + "]"
    );
}

// 再把 EditText / Button 全捞一遍（有些控件没有 text，textMatches 抓不到）
var extra = [];
var kinds = ["android.widget.EditText", "android.widget.Button", "android.widget.ImageView"];
for (var k = 0; k < kinds.length; k++) {
    var list = className(kinds[k]).find();
    for (var j = 0; j < list.size(); j++) {
        var e = list.get(j);
        var eb = e.bounds();
        extra.push(
            e.className() + "|id=" + (e.id() || "") + "|text=" + (e.text() || "") +
            "|desc=" + (e.desc() || "") + "|clickable=" + e.clickable() +
            "|[" + eb.left + "," + eb.top + "," + eb.right + "," + eb.bottom + "]"
        );
    }
}

var payload = "package=" + currentPackage() + "\nactivity=" + currentActivity() +
    "\n--- text nodes ---\n" + parts.join("\n") +
    "\n--- typed nodes ---\n" + extra.join("\n");

var res = http.postJson(BASE + "/api/phone/dump?k=" + KEY, { tag: "probe", dump: payload }, { timeout: 15000 });
toast("已上传：" + parts.length + " 个文字控件");

// 同时写一份到手机本地，方便断网时取
files.write("/sdcard/运单助手_probe.txt", payload);
