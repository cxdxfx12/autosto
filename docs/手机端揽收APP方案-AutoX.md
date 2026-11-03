# 手机端揽收 App 方案（AutoX.js）

> 目标：把现在 **PC 用 ADB 驱动手机** 的那一套（adb.exe 黑框、ADBKeyboard 广播、和「钉钉微信转发桥」抢前台）搬到 **手机本地脚本** 上。
> PC 只负责「采集 + 发单号 + 收结果」，手机自己打开申行者、粘贴、点按钮、回写。
> 本仓库的 `scan/server.py` 已经预留了对应的接口（`/api/phone/*`），这份方案就是把那批接口用 AutoX.js 接起来。

---

## 0. 为什么做这个（和现方案的关系）

现在线上跑的是 `tools/phone_run.py`：PC 通过 USB + ADB 操作手机。痛点：

| 痛点 | 根因 |
|---|---|
| `adb.exe` 黑框一直弹 | windowed 程序调控制台子进程，已用 `CREATE_NO_WINDOW` 压过，但仍依赖线 |
| 和「钉钉微信转发桥」抢前台 | 转发桥每 5/10 分钟巡检拉回钉钉，正好撞上我们点「确定」→ 整批白跑（废过 176 条） |
| 依赖 USB 线 + 电脑常开 | 电脑一休眠/断线，揽收就停 |
| ADBKeyboard 输入法来回切 | 收尾要切回华为原生，忘了日常打字没键盘 |

**手机端 App 的思路**：在手机上装一个 AutoX.js（基于 Android 无障碍服务，免 Root、免编译 APK、脚本即改即用），脚本自己轮询 PC 的 HTTP 接口拿单号 → 写剪贴板 → 打开申行者粘贴 → 点确定/完成/上传 → 回写结果。PC 端**完全不用 ADB**，只当服务端。

> 注：本仓库 `docs/方案-v2-实施.md` 是更早的「Java 无障碍服务 APK」架构（2026-10-19）。那套要写 APK、改编译，成本高。本文是它的**轻量替代**，复用同一套服务端接口和同一套「手机做笨、判断留 PC」的哲学，但用 AutoX.js 落地，开发周期从「写 APK」降到「写一个 .js 脚本」。

---

## 1. 技术选型

| 候选 | 结论 |
|---|---|
| AutoX.js / AutoJs6（无障碍脚本） | **选这个**。免 Root、免编译、自带 HTTP 客户端 + 剪贴板 + 无障碍节点 API，脚本改完秒生效 |
| 原生 Android 无障碍服务 APK | 成本高（编译/签名/保活），但最稳。等 AutoX.js 验证 OK 再考虑是否转原生 |
| Tasker / MacroDroid | 能点按钮，但抓控件树、HTTP、JSON 都弱，揽收这种有状态流程写不顺 |
| Appium / UIAutomator 远程 | 本质还是「PC 驱动手机」，没解决根因 |

一句话：**先用 AutoX.js 跑通闭环，验证申行者控件树能被无障碍拿到，再决定要不要固化成 APK。**

---

## 2. 架构与数据流

```
┌──────── PC（本仓库，已在跑） ─────────┐        ┌──────── 安卓（AutoX.js 脚本） ────────┐
│ 采集（Excel / 微信群）→ scan_jobs     │        │ 申行者 App（前台，插电常亮）          │
│ HTTP 服务 127.0.0.1:8766             │  LAN   │     ▲ setText / click（无障碍）       │
│   GET  /api/phone/next?k=TOKEN        │◄──────►│  AutoX.js 脚本：                      │
│   POST /api/phone/report              │        │   轮询拿单 → 剪贴板 → 粘贴 → 点按钮  │
│   POST /api/phone/heartbeat?k=        │        │   → 收口 → 回写结果                   │
│   POST /api/phone/dump                 │        │   coex: 让路仲裁（POST :8765/watch-pause）│
└───────────────────────────────────────┘        └──────────────────────────────────────┘
```

**一轮闭环（脚本侧）**：

1. `GET /api/phone/next?k=TOKEN` → 拿到 `{batch_id, text, count}`
2. 让路仲裁：`POST http://127.0.0.1:8765/watch-pause {"seconds":180}`（告诉转发桥这几秒前台归我，避免撞车；**可选但强烈建议**）
3. 把 `text` 写进系统剪贴板
4. 拉起申行者 `cn.sto.sxz.core.app`，进「揽件扫描 / 批量录入」页
5. 长按输入框 → 粘贴（`text` 是逗号分隔，申行者自己分行）
6. 点「确定」→ 等列表生成 → 点「完成」→ 进扫描页
7. 点「上传(N)」（resource-id `tvUpload`），盯着计数归零收口
8. 处理弹窗：拦截件点「移除并继续」；「有N个重复单号,已经过滤」= **这批已揽收**，按成功结算
9. `POST /api/phone/report`：`{batch_id, finished:true, ok:[...], fail:[{waybill,reason,final}]}`
10. 释放让路：`POST :8765/watch-pause {"seconds":0}`

---

## 3. 接口契约（直接复用 server.py 已实现的部分）

> 以下字段名以 `scan/server.py` 当前实现为准，脚本照着抄即可。

### 3.1 取单 `GET /api/phone/next?k=<TOKEN>`
返回：
```json
{ "batch_id": "B20261103-...", "date": "2026-11-03", "count": 500,
  "text": "771...,772...,", "ts": 1234567890 }
```
- `text` 是逗号分隔的单号串，申行者粘贴后会自动按分隔符分行。
- `k` 就是 `config.yaml` 的 `server.token`（局域网内使用，和界面 API 的 `X-Token` 是同一条凭据）。
- 没有待扫单时返回 `count:0, text:""`，脚本应空转等待，不要报错退出。

### 3.2 回写 `POST /api/phone/report`
```json
{ "batch_id": "B20261103-...",
  "finished": true,
  "ok":  ["771...", "772..."],
  "fail": [ {"waybill":"773...", "reason":"拦截", "final":true} ] }
```
- `ok` / `fail` 用**显式清单**模式（脚本自己知道这轮提交了哪几条，进程被杀也不会算错）。
- `fail[i].final=true`：这单注定揽不了（如拦截件被弹窗移除），后端直接落终态，不回 pending，否则会死循环。
- 回写**必须重试 3 次**：上传已提交但回写失败 → 库里还是 pending → 下一轮重发 → 申行者整批报「重复」。这是 2026-10-29 白干一轮的真实根因，PC 脚本已踩过。

### 3.3 心跳 / 调试（可选）
- `POST /api/phone/heartbeat?k=<TOKEN>&page=watch`：更新界面「手机」卡在线状态。
- `POST /api/phone/dump`：上传当前界面控件树到 `data/phone_dumps/`，排障用（脚本卡哪一屏就 dump 一份）。

---

## 4. 关键节点定位（已实测校准，照抄）

来自 `tools/phone_run.py` 的常量：

| 元素 | 怎么认 | 备注 |
|---|---|---|
| 批量输入框 | `className=android.widget.EditText` | 粘贴目标 |
| 确定 | 文案「确定」 | 录入页提交 |
| 完成 | 文案「完成」 | 录入页右上角，点了才进扫描页；**不是「上传」** |
| 上传 | resource-id 含 `tvUpload`，或文案前缀「上传」 | 扫描页底部，文字从「上传(495)」变「上传(0)」= 收口，**按钮不消失** |
| 安全页 | 「派件任务 / 签收扫描 / 请扫描运单条码」 | 可往下走的起点 |
| 首页 | 有「派件任务/揽件任务」tab 且无上传按钮 | 收尾用 HOME 键最小化，不用逐层返回 |

**弹窗词表**（和 PC 脚本一致）：
- 好词（点了继续）：`确定 / 知道了 / 我知道了 / 好的 / 关闭 / 继续 / 确认`
- 坏词（要小心）：`拦截 / 超重 / 重复 / 已揽收 / 已扫描 / 失败 / 异常 / 超过 / 限额 / 不能`
- **「重复单号,已经过滤」= 已揽收**：按用户口径（2026-10-29），整批记 ok，不再上传。

---

## 5. 共存：让路仲裁（必做）

这台机器上还有 `D:\wxauto-main\ddwx-bridge`（网关 8765）在操作同一部手机。它的自愈巡检会打开最近任务 + 拉回钉钉，撞上我们点「确定」就落空。

脚本侧要做和 `tools/phone_arbiter.py` **一样**的事：
```
碰手机前：POST http://127.0.0.1:8765/watch-pause {"seconds": 180}
收尾时：  POST http://127.0.0.1:8765/watch-pause {"seconds": 0}
```
- token 在 `ddwx-bridge/config.yaml` 的 `dingtalk.gateway_token`，脚本读一下或写死。
- 对方没开时只会失败「跳过」，不影响揽收——别把让路变成硬依赖。

---

## 6. AutoX.js 脚本骨架（可直接落进 AutoX.js IDE）

> 这是**骨架**，节点定位按 §4 的文案/resource-id 写。AutoX.js 的控件 API 与 PC 端 uiautomator 类似，下面用其常见写法示意，落地时按 AutoX.js 实际 API 微调。

```javascript
// sto_phone_run.js —— AutoX.js 揽收执行器
// 配置
const PC_BASE = "http://<电脑内网IP>:8766";   // 注意：手机访问 PC 走局域网 IP，不是 127.0.0.1
const TOKEN   = "<config server.token>";
const PKG     = "cn.sto.sxz.core.app";
const PEER    = "http://127.0.0.1:8765";       // 转发桥网关（和脚本同机？否——脚本在手机上，这里要填 PC 的 IP）
const PEER_TOKEN = "<ddwx gateway_token>";

const OK_WORDS   = ["确定","知道了","我知道了","好的","关闭","继续","确认"];
const BAD_WORDS  = ["拦截","超重","重复","已揽收","已扫描","失败","异常","超过","限额","不能"];

function httpGet(path){ /* AutoX: http.get(PC_BASE+path) → 解析 JSON */ }
function httpPost(path, body){ /* AutoX: http.postJson(PC_BASE+path, body) */ }
function peerYield(sec){ /* http.postJson(PEER+"/watch-pause", {seconds:sec, token:PEER_TOKEN}) 容错 */ }

function findNode(pred){ /* 遍历 auto.root() 或 package 节点，用 pred(node) 过滤 */ }
function tap(node){ /* node.click() 或 click(node.bounds().centerX(), node.bounds().centerY()) */ }
function setClip(text){ /* setClipboardText(text) */ }

function takeBatch(){
  const r = httpGet("/api/phone/next?k="+TOKEN);
  return r && r.count > 0 ? r : null;
}

function openSxz(){
  // 已在申行者前台则跳过；否则 launch(PKG) 并等待
  // 回到安全页（派件任务/签收扫描），清掉上一轮残留
}

function pasteAndSubmit(text){
  setClip(text);
  // 长按 EditText → 粘贴
  // 点「确定」，等列表生成
  // 点「完成」进扫描页，等「上传(N)」出现
}

function uploadAndCollect(){
  // 点 tvUpload，循环 1.2s 读一次节点
  // 计数归零 = 收口；好词+坏词同时出现 → 点掉，记录弹窗
  // 返回 {finished, ok:[], fail:[], dialogs:[]}
}

function report(batch, result){
  for (let i=0;i<3;i++){
    try {
      httpPost("/api/phone/report", {
        batch_id: batch, finished: result.finished,
        ok: result.ok, fail: result.fail
      });
      return;
    } catch(e){ sleep(2000); }
  }
  // 3 次失败：落盘到 AutoX 文件目录，提示人工补报（上传已成功，切勿重发）
}

function loop(){
  const batch = takeBatch();
  if (!batch) { sleep(10000); return; }       // 没单就空转
  peerYield(180);                              // 让路
  try {
    openSxz();
    pasteAndSubmit(batch.text);
    const res = uploadAndCollect();
    report(batch.batch_id, res);
  } finally {
    peerYield(0);                              // 释放
  }
}

// 常驻：定时或事件触发，建议每 30s 轮一次，避免一直占前台
while (true){ loop(); sleep(30000); }
```

**落地时必填的 3 个值**：`PC_BASE` 的局域网 IP、`TOKEN`、`PEER_TOKEN`。其余节点定位照 §4。

---

## 7. 与现 PC-ADB 方案对比

| 维度 | 现方案（PC 驱动 ADB） | 手机端 App（AutoX.js） |
|---|---|---|
| 依赖 | USB 线 + 电脑常开 + adb | 只有局域网，手机插电常亮即可 |
| 黑框/输入法 | adb.exe 黑框、ADBKeyboard 切换 | 无，直接无障碍操作 |
| 抢前台 | 和转发桥激烈冲突，要仲裁 | 同样要仲裁，但少一层 ADB 占用 |
| 改脚本 | 要进 tools/ 或重打包 | AutoX.js 里改完即生效 |
| 稳定性 | 已踩平大部分坑（1.1.4） | 新写，要重新验控件树 |
| 风险 | 已知 | 申行者无障碍树是否好抓、AutoX.js 保活 |

---

## 8. 落地步骤（P0 → 跑通）

1. **P0 探针（0.5h）**：手机装 AutoX.js，开无障碍 + 悬浮窗权限；写个 5 行脚本 dump 一次申行者批量录入页的控件树，确认 §4 的文案/resource-id 能对上。
2. **P1 手动闭环（半天）**：先不轮询，手动触发 `takeBatch` 一次 → 粘贴 → 点按钮 → 回写，验证 `text` 格式和收口判据。
3. **P2 自动化（1 天）**：接上轮询 + 让路仲裁 + 弹窗处理 + 回写重试，跑 50 条真实单，成功率 ≥95%。
4. **P3 收尾（可选）**：常驻保活、失败截图、和 PC 界面「手机」卡状态打通（心跳）。

---

## 9. 风险与待办

- **申行者无障碍树**：AutoX.js 抓不到节点就得回退原生 APK 或巴枪方案（见 `方案-v2-实施.md` §8）。
- **AutoX.js 保活**：华为后台杀进程猛，要设「允许后台」「电池优化白名单」「无线充电常亮」。
- **让路仲裁的 IP**：脚本在手机上，`PEER` 和 `PC_BASE` 都必须填 PC 的**局域网 IP**，不是 `127.0.0.1`。
- **token 安全**：脚本里写死 token 只限本机局域网，别外泄；要换就改 `config.yaml` 的 `server.token` 同步到脚本。
- 验证顺序：先在 AutoX.js 里手动跑一条，确认 `text` 能粘贴、收口能识别、回写能结算，再上常驻轮询。
