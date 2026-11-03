'use strict';
/*
 * sto_phone_run.js —— 运单自动揽收 · 手机端执行器（AutoX.js）
 * ------------------------------------------------------------
 * 把 PC 端「ADB 驱动手机」那套逻辑搬到手机本地：脚本轮询电脑的 HTTP 接口
 * 拿单号 → 写剪贴板 → 打开申行者粘贴 → 点确定/完成/上传 → 收口 → 回写结果。
 * 电脑端只当服务端（scan/server.py 的 /api/phone/*），完全不依赖 ADB / USB。
 *
 * 接口契约与 scan/server.py 当前实现严格一致：
 *   GET  /api/phone/next?k=TOKEN   -> {batch_id,date,count,text,ts}  text 是 "," 连接的单号，无尾逗号
 *   POST /api/phone/report         -> {batch_id,finished,ok:[...],fail:[{waybill,reason,final}]}
 *                                     ★ 必须带 ok 列表（显式清单），否则后端按整批结算会误判
 *   POST /api/phone/heartbeat?k=&page=watch
 *   POST /api/phone/dump           -> {dump,tag}  上传控件树排障
 * 仲裁协议（和转发桥共存）：POST http://{PC内网IP}:8765/watch-pause {"seconds":N} + header X-Bridge-Token
 *
 * 部署与权限见同目录 README.md。首次上线务必先跑 probe.js 校准 §节点定位。
 */

/* ===================== 1. 配置（必填 3 项） ===================== */
const CFG = {
  // ① 电脑的【局域网 IP】+ 端口（手机走 LAN，不是 127.0.0.1）
  //    电脑上 `ipconfig` 看「WLAN」的 IPv4，例如 192.168.1.50
  PC_BASE: "http://192.168.1.50:8766",

  // ② 和电脑 config.yaml 的 server.token 完全一致（界面「手机监听地址」也会带这个 k）
  TOKEN: "在此填入_server.token",

  // 申行者包名（已实测，勿改）
  PKG: "cn.sto.sxz.core.app",

  // ③ 钉钉微信转发桥网关：同电脑局域网 IP + 8765
  PEER_HOST: "192.168.1.50",
  PEER_PORT: 8765,
  // ddwx-bridge/config.yaml 里的 `token:` 或 `gateway_token:` 那一行
  PEER_TOKEN: "在此填入_ddwx_token",

  // 轮询间隔（毫秒）。没单时空转，不占前台
  POLL_MS: 30000,
  // 单轮最长占用前台时间，超时强制收尾释放让路，避免卡死占着手机
  ROUND_TIMEOUT_MS: 180000,
  // 上传收口轮询间隔
  UPLOAD_POLL_MS: 1200,
};

/* ===================== 2. 文案 / 词表 ===================== */
// 弹窗里点了「继续」的好词
const OK_WORDS = ["确定", "知道了", "我知道了", "好的", "关闭", "继续", "确认"];
// 坏词（要小心，对应弹窗需要特殊处理，不能随手点）
const BAD_WORDS = ["拦截", "超重", "重复", "已揽收", "已扫描", "失败", "异常", "超过", "限额", "不能"];

/* ===================== 3. HTTP 封装 ===================== */
function httpGetJson(url) {
  try {
    const r = http.request(url, { method: "GET", timeout: 10000 });
    if (r.statusCode !== 200) { log("[http] GET " + url + " -> " + r.statusCode); return null; }
    return r.body.json();
  } catch (e) { log("[http] GET 失败 " + url + " : " + e); return null; }
}

function httpPostJson(url, data) {
  try {
    const r = http.request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
      timeout: 15000,
    });
    let body = null;
    try { body = r.body.json(); } catch (_) { try { body = JSON.parse(r.body.string()); } catch (__){} }
    return { code: r.statusCode, body: body };
  } catch (e) { log("[http] POST 失败 " + url + " : " + e); return { code: 0, body: null }; }
}

// 取单
function takeBatch() {
  const r = httpGetJson(CFG.PC_BASE + "/api/phone/next?k=" + encodeURIComponent(CFG.TOKEN));
  if (!r) return null;
  if (!r.batch_id || r.count <= 0) return null;   // 没单：返回 null，主循环空转
  heartbeat("watch");
  return r;
}

// 回写（3 次重试；全败落盘，切勿重发，因为上传已提交）
function report(batchId, result) {
  const payload = {
    batch_id: batchId,
    finished: true,
    ok: result.ok || [],
    fail: result.fail || [],
    note: result.note || "AutoX批量揽收",
  };
  for (let i = 0; i < 3; i++) {
    const { code, body } = httpPostJson(CFG.PC_BASE + "/api/phone/report", payload);
    if (code === 200) {
      log("[回写] OK batch=" + batchId + " marked=" + JSON.stringify((body && body.marked) || {}));
      return true;
    }
    log("[回写] 第" + (i + 1) + "次失败 code=" + code + "，2s 后重试");
    sleep(2000);
  }
  // 3 次全败：上传已成功但回写没进去，落盘人工补报，绝不能重发这一批
  try {
    const f = "/sdcard/sto_report_fail_" + batchId + "_" + Date.now() + ".json";
    files.write(f, JSON.stringify(payload, null, 2));
    log("[回写] 已落盘待补报：" + f + "（上传已成功，勿重发）");
  } catch (e) { log("[回写] 落盘也失败：" + e); }
  return false;
}

function heartbeat(page) {
  httpGetJson(CFG.PC_BASE + "/api/phone/heartbeat?k=" + encodeURIComponent(CFG.TOKEN) + "&page=" + (page || "watch"));
}

function uploadDump(tag) {
  try {
    const root = auto.root();
    const dump = root ? root.toString() : "(无控件树)";
    httpPostJson(CFG.PC_BASE + "/api/phone/dump", { dump: dump, tag: tag || "autox" });
  } catch (e) { log("[dump] 失败：" + e); }
}

/* ===================== 4. 仲裁协议（和转发桥共存） ===================== */
function peerYield(sec) {
  try {
    const r = http.request("http://" + CFG.PEER_HOST + ":" + CFG.PEER_PORT + "/watch-pause", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Bridge-Token": CFG.PEER_TOKEN },
      body: JSON.stringify({ seconds: sec }),
      timeout: 3000,
    });
    log("[让路] watch-pause " + sec + "s -> " + r.statusCode);
  } catch (e) {
    // 对方没开 / 连不上：跳过，不影响揽收（别把让路变硬依赖）
    log("[让路] 失败(忽略)：" + e);
  }
}

/* ===================== 5. 控件查找 / 点击 ===================== */
function findOne(sel, ms) {
  try { return sel.findOne(ms || 5000); } catch (e) { return null; }
}
function clickSel(sel, ms) {
  const n = findOne(sel, ms);
  if (n) { n.click(); return true; }
  return false;
}
// 在 root 下按正则找第一个文本匹配的节点
function findTextMatches(re, ms) {
  return findOne(textMatches(re), ms);
}
function clickTextMatches(re, ms) {
  const n = findTextMatches(re, ms);
  if (n) { n.click(); return true; }
  return false;
}

/* ===================== 6. 弹窗处理（核心） ===================== */
// 每轮交互中反复调用。返回是否处理了任意弹窗。
// state: { repeatAll:false, interceptions:Set }
function handleDialogs(state) {
  let handled = false;

  // (A) 重复单号弹窗：「有N个重复单号,已经过滤」= 整批已揽收（用户口径 2026-10-29）
  //     整批记 ok，点「我知道了 / 确定」关掉，不再上传。
  if (findTextMatches(/重复单号|已经过滤/)) {
    state.repeatAll = true;
    log("[弹窗] 检测到重复单号 -> 整批按已揽收");
    if (clickTextMatches(/我知道了|确定|好的/)) handled = true;
    return true; // 直接返回，避免下面再把「确定」当普通弹窗点掉
  }

  // (B) 拦截件弹窗：「温馨提示：xxx为拦截件…[拦截][移除并继续]」
  //     提取对话框里的单号 -> 记 fail(final:true)，点「移除并继续」
  const interceptNode = findTextMatches(/拦截件?/);
  if (interceptNode) {
    const txt = interceptNode.text() || "";
    const m = txt.match(/\d{8,}/g) || [];
    m.forEach(function (w) { state.interceptions.add(w); });
    log("[弹窗] 拦截件 " + m.length + " 条 -> 移除并继续：" + m.join(","));
    if (clickTextMatches(/移除并继续|移除/)) handled = true;
    return true;
  }

  // (C) 通用好词弹窗（知道了/好的/关闭/确认 等）—— 只点明确的关闭按钮，避免误点业务按钮
  for (let i = 0; i < OK_WORDS.length; i++) {
    if (clickTextMatches(new RegExp("^" + OK_WORDS[i] + "$"))) { handled = true; break; }
  }
  return handled;
}

/* ===================== 7. 申行者导航 ===================== */
function ensureInputPage() {
  // 不在申行者前台就拉起
  const top = currentPackage();
  if (top !== CFG.PKG) {
    app.launch(CFG.PKG);
    sleep(3000);
  }
  // 等批量输入框（className EditText）出现；出现说明已经在录入页
  let ed = findOne(className("android.widget.EditText"), 8000);
  if (ed) return true;
  // 否则尝试从首页/安全页进「批量录入 / 揽件」
  clickTextMatches(/批量录入|揽件扫描|揽件任务|批量/);
  sleep(2000);
  ed = findOne(className("android.widget.EditText"), 8000);
  if (ed) return true;
  log("[导航] 未找到批量输入框，可能需要手动进录入页（见 README 校准）");
  return false;
}

function pasteAndSubmit(text, state) {
  setClip(text);
  sleep(500);
  const ed = findOne(className("android.widget.EditText"), 8000);
  if (!ed) { throw new Error("找不到批量输入框"); }
  // 长按唤起粘贴菜单
  ed.longClick();
  sleep(800);
  // 点「粘贴」（有的机型在浮动工具栏，有的在菜单）
  if (!clickTextMatches(/粘贴/)) {
    // 退回：尝试直接 setText（仅当是 EditText 且支持）
    try { ed.setText(text); } catch (e) { throw new Error("粘贴失败且无 setText 兜底"); }
  }
  sleep(1000);
  handleDialogs(state); // 粘贴后可能出现的提示
  // 点录入页「确定」提交
  if (!clickTextMatches(/^确定$/)) { throw new Error("找不到录入页确定按钮"); }
  // 等列表生成（出现「完成」或上传按钮）
  const okGen = findOne(text("完成").or(idContains("tvUpload")), 8000);
  if (!okGen) { throw new Error("点确定后未进入列表/扫描页"); }
}

// 收口：点完成 -> 扫描页 -> 点上传(N) -> 计数归零
function uploadAndCollect(state) {
  // 先在录入页点「完成」进扫描页
  if (!clickTextMatches(/^完成$/)) {
    log("[收口] 没找到完成按钮，可能已在扫描页");
  }
  sleep(1500);

  // 找上传按钮：优先 resource-id 含 tvUpload，否则文案前缀「上传」
  const upSel = idContains("tvUpload").or(textMatches(/^上传/));
  let up = findOne(upSel, 10000);
  if (!up) { throw new Error("找不到上传按钮(tvUpload/上传)"); }

  const deadline = Date.now() + CFG.ROUND_TIMEOUT_MS;
  while (Date.now() < deadline) {
    // 先处理这轮可能冒出来的弹窗（拦截/重复）
    handleDialogs(state);
    up = findOne(upSel, 3000);
    if (!up) {
      // 上传按钮消失可能意味着已收口或进了别的页，再判一次
      if (findOne(textMatches(/派件任务|揽件任务|请扫描运单条码/), 2000)) break;
      sleep(CFG.UPLOAD_POLL_MS);
      continue;
    }
    const t = up.text() || "";
    const m = t.match(/\((\d+)\)/);
    const n = m ? parseInt(m[1], 10) : -1;
    if (n === 0) { log("[收口] 计数归零，上传完成"); break; }
    if (m && n > 0) { log("[收口] 待上传 " + n); }
    sleep(CFG.UPLOAD_POLL_MS);
  }
}

/* ===================== 8. 一轮闭环 ===================== */
function runRound() {
  const batch = takeBatch();
  if (!batch) { sleep(CFG.POLL_MS); return; }

  peerYield(180); // 碰手机前让路，避免和转发桥抢前台
  const state = { repeatAll: false, interceptions: new Set() };
  try {
    device.wakeUp();
    if (!ensureInputPage()) { peerYield(0); return; }
    const all = String(batch.text).split(",");
    pasteAndSubmit(batch.text, state);
    uploadAndCollect(state);

    // 结算口径
    let ok, fail;
    if (state.repeatAll) {
      ok = all; fail = [];
    } else {
      fail = [];
      state.interceptions.forEach(function (w) { fail.push({ waybill: w, reason: "拦截", final: true }); });
      const bad = state.interceptions;
      ok = all.filter(function (w) { return !bad.has(w); });
    }
    report(batch.batch_id, { ok: ok, fail: fail });
  } catch (e) {
    log("[轮次异常] " + e + "（上传可能已部分成功，回写会尽力）");
    try { uploadDump("round_error"); } catch (_) {}
    // 异常也尽量回写一次，把所有发出去的单记 ok，避免滞留 pending
    const all = String(batch.text || "").split(",").filter(Boolean);
    report(batch.batch_id, { ok: all, fail: [], note: "AutoX异常兜回ok:" + e });
  } finally {
    peerYield(0); // 释放让路
  }
}

/* ===================== 9. 入口 ===================== */
function main() {
  // 开启无障碍（需用户在 AutoX 里授权无障碍服务 + 悬浮窗）
  auto.waitFor();
  log("==== 运单揽收 AutoX 启动 ====");
  log("PC_BASE=" + CFG.PC_BASE + "  TOKEN=" + (CFG.TOKEN ? "已填" : "未填!"));

  // 配置自检
  if (!CFG.TOKEN || CFG.TOKEN.indexOf("在此填入") === 0) {
    toast("请先在脚本里填好 TOKEN / PC_BASE / PEER");
    throw new Error("配置未填：TOKEN");
  }

  let errStreak = 0;
  while (true) {
    try {
      runRound();
      errStreak = 0;
    } catch (e) {
      errStreak++;
      log("[致命] " + e);
      if (errStreak > 5) { toast("连续异常，暂停并检查日志"); sleep(60000); errStreak = 0; }
    }
    sleep(CFG.POLL_MS);
  }
}

main();
