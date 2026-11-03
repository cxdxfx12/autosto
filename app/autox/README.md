# 手机端揽收执行器（AutoX.js）

把 PC 端「ADB 驱动手机」那套逻辑搬到**手机本地脚本**：脚本轮询电脑的 HTTP 接口拿单号 → 写剪贴板 → 打开申行者粘贴 → 点确定 / 完成 / 上传 → 收口 → 回写结果。电脑端（`scan/server.py` 的 `/api/phone/*`）只当服务端，**不再依赖 ADB / USB 线**。

> 架构与协议细节见仓库根 `docs/手机端揽收APP方案-AutoX.md`。本目录是可运行的落地代码。

## 文件

| 文件 | 作用 |
|---|---|
| `sto_phone_run.js` | 主执行器：常驻轮询 + 揽收闭环 + 让路仲裁 + 回写重试 |
| `probe.js` | P0 探针：dump 申行者控件树，校准节点定位 |

## 前置条件

- 一部安卓手机（已实测华为 PAL-AL00），装好**申行者** App（包名 `cn.sto.sxz.core.app`）。
- 电脑和手机在**同一局域网**（同一 WiFi / 热点）。
- 电脑端程序已启动，且 `config.yaml` 的 `server.token` 已设（界面「手机监听」卡片会显示带 `k=` 的地址）。
- 电脑上另有一套「钉钉微信转发桥」（`ddwx-bridge`，网关 8765）在操作同一部手机时，需要它的 `token` 做让路仲裁（见下）。

## 安装 AutoX.js

AutoX.js 是开源的无障碍脚本引擎（免 Root、免编译 APK）。

1. 从官方渠道下载 AutoX.js APK 并安装（注意：应用商店里名字相近的"Auto.js"已停更，请用 AutoX.js / AutoJs6 分支）。
2. 打开 AutoX.js → 开启**无障碍服务**（设置里授权）。
3. 开启**悬浮窗权限**（用于运行时日志面板）。
4. 建议把 AutoX.js 自身也加入电池白名单（见保活）。

## 填 3 个配置值（必做）

打开 `sto_phone_run.js` 顶部的 `CFG` 块，改这三项：

```javascript
PC_BASE: "http://192.168.1.50:8766",   // ① 电脑局域网 IP + 端口（手机走 LAN，不是 127.0.0.1）
TOKEN:   "xxxx",                        // ② 电脑 config.yaml 的 server.token
PEER_TOKEN: "yyyy",                     // ③ ddwx-bridge/config.yaml 的 token（没有转发桥可留空串）
```

- ① 电脑上 `ipconfig`（Windows）/ `ifconfig`（Mac/Linux）查 WLAN 的 IPv4。
- ② 和界面显示的手机监听地址里的 `k=` 一致。
- ③ 没有转发桥运行时，`PEER_TOKEN` 留空串 `""` 即可，脚本会自动跳过让路。

## 落地步骤（P0 → 跑通）

**P0 校准（必做，约 10 分钟）**
1. 手机手动打开申行者，进到「批量录入」页（有那个大输入框的页面）。
2. 在 AutoX.js 里运行 `probe.js`，日志面板会打印整棵控件树，同时存到 `/sdcard/sto_probe_*.txt`。
3. 对照打印结果确认：`sto_phone_run.js` 里的 `className("android.widget.EditText")`（输入框）、`^确定$`、`^完成$`、`idContains("tvUpload")`（上传按钮）、弹窗词表是否对得上。不对就改脚本里的选择器。

**P1 手动闭环（半天）**
1. 电脑端确保有当天待揽收单（`/api/stats` 看 pending）。
2. AutoX.js 运行 `sto_phone_run.js`，观察：取单 → 粘贴 → 点确定 → 完成 → 上传计数归零 → 回写。
3. 重点验证：单号能粘进申行者、`上传(N)` 计数能归零识别、回写后电脑端该批变 ok。

**P2 自动化（1 天）**
- 接上常驻轮询（脚本默认 `while(true)` 每 30s 一轮）。
- 跑 50 条真实单，成功率 ≥95% 再常态化。

**P3 收尾（可选）**
- 常驻保活、失败截图、心跳已打通（脚本每轮 `heartbeat("watch")` 更新电脑界面「手机」卡在线）。

## 华为保活（关键，否则后台被杀）

华为后台杀进程很猛，务必设置：
- **设置 → 应用 → AutoX.js → 电池 → 改为「不允许」/ 不受限制**（关掉电池优化）。
- **设置 → 应用启动管理 → AutoX.js → 手动管理 → 允许自启动 / 关联启动 / 后台运行**。
- 手机**插电常亮**（或用无线充电），息屏后无障碍服务容易掉。
- AutoX.js 里打开「音量键停止」之外的保活开关（如有）。

## 与「钉钉微信转发桥」共存

转发桥（`ddwx-bridge`，网关 `:8765`）每隔几分钟会拉回钉钉，正好撞上我们点「确定」会落空。脚本已内置让路仲裁：

```
碰手机前：peerYield(180)  -> POST http://{电脑IP}:8765/watch-pause {"seconds":180}  + header X-Bridge-Token
收尾时：  peerYield(0)    -> 同上 {"seconds":0}
```

- 转发桥没开时脚本只会打印「让路失败(忽略)」，**不影响揽收**——别把让路当硬依赖。
- `PEER_TOKEN` 取 `ddwx-bridge/config.yaml` 的 `token:` / `gateway_token:` 那一行。

## 弹窗处理口径（和 PC 端一致）

| 弹窗 | 处理 |
|---|---|
| `有N个重复单号,已经过滤` | 整批按**已揽收**记 ok（用户口径 2026-10-29），点「我知道了」 |
| `xxx为拦截件…[移除并继续]` | 提取对话框里的单号，记 `fail(final:true)`，点「移除并继续」 |
| 通用提示（知道了/好的/关闭/确认） | 点掉关闭 |

## 排障

- **取不到单**：先确认电脑端程序在跑、`/api/stats` 有 pending、手机和电脑同一 WiFi、PC_BASE 的 IP 对、`TOKEN` 对。
- **粘贴不进去**：机型差异，脚本已带 `ed.setText(text)` 兜底；仍不行就 P0 探针看输入框 className 是否变了。
- **上传计数识别不了**：`sto_phone_run.js` 里 `upSel` 优先 `idContains("tvUpload")`，再退 `^上传` 文案；P0 探针确认实际 id。
- **控件树抓不到**：确认 AutoX.js 无障碍服务已开；华为偶尔要重开一次无障碍。
- **回写失败落盘**：上传已成功但回写 3 次失败，脚本会把 payload 落到 `/sdcard/sto_report_fail_*.json`，**切勿重发这一批**，手动在电脑端界面补报即可。
