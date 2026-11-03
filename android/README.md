# 运单揽收 · 安卓 APK（原生 AccessibilityService）

把「手机端揽收」做成**真正的 Android APK**（无障碍服务方案），不再依赖 AutoX.js 脚本或 ADB/USB 线。
功能和 `app/autox/` 的 AutoX.js 脚本**完全一致**，只是换成了原生 Java 工程，用 Android Studio 一键构建成可安装的 APK。

> 架构与接口契约见仓库根 `docs/手机端揽收APP方案-AutoX.md`。本目录是可构建的 APK 工程源码。

## 和 AutoX.js 方案的关系

| | AutoX.js 脚本 (`app/autox/`) | 本原生 APK (`android/`) |
|---|---|---|
| 形态 | 在手机装 AutoX.js 后跑 .js | 独立 APK，安装即用，无需 AutoX |
| 构建 | 不用构建 | Android Studio 构建（需要 SDK/JDK） |
| 依赖 | AutoX.js 运行时 | 零第三方依赖，纯 Android 框架 |
| 适合 | 临时/调试、免构建 | 长期部署、给别人装 |

两者走同一套服务端接口（`scan/server.py` 的 `/api/phone/*`）和同一套弹窗口径，逻辑等价。

## 目录结构

```
android/
  settings.gradle / build.gradle / gradle.properties
  gradle/wrapper/gradle-wrapper.properties   # 仅 wrapper 配置；CI 用 gradle-build-action 指定 Gradle 8.4，无需 wrapper.jar
  app/
    build.gradle
    proguard-rules.pro
    src/main/
      AndroidManifest.xml
      res/  (布局/字符串/无障碍配置)
      java/com/sto/collector/
        MainActivity.java        配置UI + 启停 + 开无障碍设置
        CollectorService.java    前台服务 + 30s 轮询
        StoAccessibilityService.java  无障碍动作 + 申行者导航 + 弹窗 + 收口
        Http.java                HttpURLConnection 客户端（org.json）
        Config.java              SharedPreferences 配置
        LogBus.java              日志总线（logcat + UI）
```

## 构建（生成 APK）

两种方法，任选其一：

### 方法一：一键云端构建（推荐，不用装任何东西）

仓库已配好 GitHub Actions（`.github/workflows/build-apk.yml`），云端 Ubuntu runner 自带 JDK 17，自动装 Android SDK（android-33）+ Gradle 8.4，构建出 debug APK 并作为 Artifact 提供下载。

1. 打开 `https://github.com/cxdxfx12/autosto/actions/workflows/build-apk.yml`
2. 点 **Run workflow** → 选 `main` 分支 → Run。
3. 等 3~6 分钟，构建完成后到 **Artifacts** 区下载 `sto-collector-app-debug`（里面是 `app-debug.apk`）。
4. 把 APK 拷到手机安装。

> 每次 `android/**` 有改动 push 到 main 也会自动触发一次构建（方便拿到最新包）。
> 该 APK 是 **debug 签名**，真机可直接装（允许「未知来源」）。若要正式分发/上架，再配 release 签名。

### 方法二：本地 Android Studio

需要：**Android Studio**（自带 SDK/AGP/JDK）、**申行者**已装在手机上、手机与电脑同一局域网。

1. 用 Android Studio 打开本 `android/` 目录（File → Open）。首次会下载 AGP 8.1.4 / Gradle 8.4（需联网一次）。
2. 等待 Gradle Sync 完成。
3. 菜单 **Build → Build Bundle(s) / APK(s) → Build APK(s)**。
4. 产物在 `app/build/outputs/apk/debug/app-debug.apk`（或 release，需自己配签名）。
5. 把 APK 拷到手机安装。

> 命令行：`gradle wrapper` 生成 wrapper.jar 后 `./gradlew assembleDebug`。

## 使用步骤

1. 电脑端程序已启动，`config.yaml` 的 `server.token` 已设（界面「手机监听」卡片会显示带 `k=` 的地址），且电脑有当天待揽收单。
2. 手机安装 APK，打开「运单揽收」App。
3. 点 **打开无障碍设置** → 在系统无障碍里开启「运单揽收」服务（必须，否则脚本没法动申行者）。
4. 填 4 项配置：
   - **电脑局域网地址**：`http://电脑内网IP:8766`（手机走 LAN，不是 127.0.0.1；电脑 `ipconfig` 查 WLAN 的 IPv4）
   - **server.token**：和电脑 config.yaml 一致
   - **转发桥网关 IP**：同电脑 IP（没有转发桥可留空）
   - **转发桥 token**：`ddwx-bridge/config.yaml` 的 `token`/`gateway_token`（没有留空）
5. 点 **开始揽收**。App 会常驻前台服务，每 30s 轮询一次，有单就自动：取单 → 粘贴进申行者 → 点确定/完成/上传 → 收口 → 回写。

## 协议（与 server.py 严格一致）

- `GET  /api/phone/next?k=TOKEN` → `{batch_id,date,count,text,ts}`，`text` 是 `,` 连接的单号（无尾逗号）
- `POST /api/phone/report` → `{batch_id,finished,ok:[...],fail:[{waybill,reason,final}]}`，**必带 ok 显式清单**
- `POST /api/phone/heartbeat?k=&page=watch`
- 让路仲裁：`POST http://{电脑IP}:8765/watch-pause {"seconds":N}` + header `X-Bridge-Token`（转发桥没开会自动跳过）

## 弹窗口径（和 PC 端一致）

| 弹窗 | 处理 |
|---|---|
| `有N个重复单号,已经过滤` | 整批按**已揽收**记 ok，点「我知道了」 |
| `xxx为拦截件…[移除并继续]` | 提取单号记 `fail(final:true)`，点「移除并继续」 |
| 通用提示（知道了/好的/关闭/确认/继续） | 点掉关闭 |

## 华为保活（关键）

- 设置 → 应用 → 运单揽收 → 电池 → 不受限制（关电池优化）。
- 设置 → 应用启动管理 → 运单揽收 → 手动管理 → 允许自启动/关联启动/后台运行。
- 手机插电常亮（息屏后无障碍服务易掉）。

## 已知局限（务必读）

- **设备校准**：`StoAccessibilityService` 里的节点定位（输入框 className、`^确定$`、`^完成$`、`tvUpload`、`粘贴`、弹窗词）是按 PC 端逻辑移植的，不同申行者版本/机型可能需要微调。安装后若揽收不动，先用 AutoX 的 `probe.js` 思路 dump 控件树核对，或直接 logcat 看 `StoCollector` 标签的日志。
- **AccessibilityNodeInfo 回收**：为简化，部分搜索路径未严格回收中间节点，长期运行有轻微泄漏风险；正式上线前建议按 Android 规范补 `recycle()`。
- **编译验证**：源码可由 GitHub Actions 云端构建验证（见上文「方法一」自动出包，首次运行约 3~6 分钟）；若你本地用 Android Studio 构建报小错，多半是 SDK 版本/依赖下载问题，按提示处理即可。

## 故障排查

- 日志：手机上 App 界面有日志框；也可用 `adb logcat -s StoCollector` 看完整日志。
- 取不到单：确认电脑程序在跑、有 pending、手机电脑同 WiFi、`PC_BASE` 的 IP 和 `TOKEN` 对。
- 无障碍服务开启后仍不动：确认申行者已装、App 未被系统杀后台、已插电常亮。
- 回写失败：上传已成功但回写 3 次失败，App 会把 payload 落到 `getFilesDir()` 下的 `sto_report_fail_*.json`，**切勿重发这一批**，在电脑端界面手动补报。
