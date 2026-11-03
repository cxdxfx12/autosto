'use strict';
/*
 * probe.js —— 申行者控件树探针（P0 校准用）
 * ------------------------------------------------------------
 * 用途：在手机上打开 AutoX.js，手动进到申行者「批量录入」页，
 * 然后运行本脚本，它会把当前界面的控件树打出来，方便你确认：
 *   - 批量输入框的 className / id
 *   - 「确定」「完成」按钮的文案
 *   - 上传按钮的 resource-id（应含 tvUpload）和文案
 *   - 各种弹窗的文案（拦截件 / 重复单号）
 * 对照 sto_phone_run.js 的 §节点定位 校准即可。
 *
 * 用法：
 *   1. 手机装好 AutoX.js，开「无障碍服务」+「悬浮窗」。
 *   2. 手动打开申行者，进到批量录入页（有那个大输入框的页面）。
 *   3. AutoX.js 里「运行」本文件，日志面板会打印控件树。
 *   4.（可选）把 PKG 改成你的申行者包名后，直接运行本脚本也会自动拉起并 dump。
 */

const PKG = "cn.sto.sxz.core.app";

function dumpTree(root, depth, out) {
  if (!root) return;
  const kids = root.children();
  for (let i = 0; i < kids.size(); i++) {
    const n = kids.get(i);
    const t = (n.text() || "").trim();
    const d = (n.desc() || "").trim();
    const id = (n.id() || "").trim();
    const cls = (n.className() || "").trim();
    const clickable = n.clickable();
    const pad = "  ".repeat(depth);
    if (t || d || id) {
      out.push(pad + "[text=" + JSON.stringify(t) + " desc=" + JSON.stringify(d) +
               " id=" + JSON.stringify(id) + " cls=" + cls +
               (clickable ? " clickable" : "") + "]");
    }
    dumpTree(n, depth + 1, out);
  }
}

function main() {
  auto.waitFor();
  // 若前台不是申行者，拉起它（你再手动进录入页后重跑也行）
  if (currentPackage() !== PKG) {
    app.launch(PKG);
    toast("已拉起申行者，请手动进「批量录入」页后重跑 probe");
    sleep(3000);
  }
  sleep(1500);
  const root = auto.root();
  const out = [];
  dumpTree(root, 0, out);
  const text = out.join("\n");
  log("==== 控件树（" + out.length + " 节点）====");
  log(text);
  // 同时落盘，方便拷到电脑看
  try {
    const f = "/sdcard/sto_probe_" + Date.now() + ".txt";
    files.write(f, text);
    toast("控件树已存：" + f);
  } catch (e) { log("落盘失败：" + e); }
}

main();
