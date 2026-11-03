"""运单揽件助手 —— Windows 桌面程序入口。

    python desktop.py          # 开发模式直接跑
    运单揽件助手.exe           # PyInstaller 打包后

行为：
  1. 单实例：已有实例在跑时弹窗提示后退出
  2. 启动 HTTP 服务（线程）+ 微信采集（线程，配置里开了才会启）
  3. 有 WebView2 时显示内嵌窗口（标准桌面程序外观）
     没有时自动降级：打开默认浏览器 + 托盘图标（右键退出）

数据（config.yaml、data/、inbox/）始终在 exe 同级目录，升级覆盖 exe 不动数据。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import webbrowser
from pathlib import Path

APP_TITLE = "运单自动采集自动揽收助手"
COMPANY = "杭州喵喵至家网络有限公司"
MUTEX_NAME = "MiaoScanAssistant_SingleInstance"

# 包内自带的浏览器内核目录（相对 exe）。名字随便，找到 msedgewebview2.exe 就算命中
BUNDLED_BROWSER_DIRS = ("webview2", "runtime/webview2", "browser/webview2")


def _app_base() -> Path:
    """exe 所在目录（数据目录）；开发模式下是项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_base() -> Path:
    """只读资源（页面静态文件）；PyInstaller 解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", _app_base()))
    return Path(__file__).resolve().parent


def _patch_paths() -> None:
    """冻结后 ROOT 指到 exe 旁，静态资源指到解包目录。

    必须在 import scan.* 之前完成，让所有模块读到改过的值。
    """
    base, res = _app_base(), _resource_base()
    import scan.config as cfgmod
    import scan.rules as rulesmod
    import scan.server as srvmod

    cfgmod.ROOT = base
    rulesmod.CONFIG_PATH = base / "config.yaml"
    srvmod.WEB_DIR = res / "app" / "web"


def _single_instance() -> bool:
    """返回 True 表示本实例是「第一个」。

    注意：必须用 use_last_error=True + ctypes.get_last_error()，
    直接调 kernel32.GetLastError 会被 ctypes 自己的调用清掉，永远不准。
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _alert(msg: str) -> None:
    ctypes.windll.user32.MessageBoxW(0, msg, APP_TITLE, 0x40)  # MB_ICONINFORMATION


def _start_background(config, service):
    """启动微信采集线程 + HTTP 服务，返回前台要用的 httpd。"""
    from scan.rules import RuleStore
    from scan.server import make_server

    import run  # 复用 CLI 的启动逻辑（微信线程已处理 COM 初始化）

    rules = RuleStore(config)

    def starter() -> None:
        if rules.group_name:
            run._start_wechat_thread(config, service, rules)

    # 采集线程常驻，受服务总开关控制（关着时只睡觉，不碰微信）
    run._start_wechat_thread(config, service, rules)
    run._start_auto_thread(config, service)
    return make_server(config, rules, wechat_starter=starter)


def _bundled_webview2() -> Path | None:
    """包内自带的浏览器内核（WebView2 固定版）。有就用它，彻底不依赖系统环境。"""
    base = _app_base()
    for rel in BUNDLED_BROWSER_DIRS:
        d = base / rel
        if (d / "msedgewebview2.exe").exists():
            return d
    return None


def _run_webview(httpd, url: str) -> None:
    import webview

    runtime = _bundled_webview2()
    if runtime:
        # 关键一行：让 WebView2 用包内内核，而不是去找系统装的运行时
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(runtime)
        _say(f"浏览器内核：包内 {runtime.name}")
    else:
        _say("浏览器内核：系统 WebView2 运行时")

    # 注意：不要传 background/background_color —— 各版本 pywebview 支持不一致，
    # 传错会直接 TypeError，然后一路退化成系统浏览器（踩过这个坑）。
    webview.create_window(
        APP_TITLE,
        url,
        width=1180,
        height=820,
        min_size=(940, 620),
    )
    # pywebview 自己挑后端：有 WebView2 用 EdgeChromium，没有则退回 MSHTML
    import os as _os

    if _os.environ.get("MIAO_REMOTE_DEBUG"):
        # 排障口：设了就把 DevTools 协议开在这个端口
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(_os.environ["MIAO_REMOTE_DEBUG"])

    webview.start(
        gui="edgechromium",
        # 缓存放程序目录，别塞进 C:\Users\...\AppData 里（卸载时不好清）
        storage_path=str(_app_base() / "data" / "webview"),
        icon=str(_resource_base() / "app" / "assets" / "app.ico"),
        debug=bool(_os.environ.get("MIAO_DEBUG")),
    )

    import os

    httpd.shutdown()
    os._exit(0)


def _has_webview2() -> bool:
    try:  # pywebview 5.x 自带检测
        from webview.platforms.edgechromium import WebView2Loader  # noqa: F401

        return True
    except Exception:
        try:
            import webview.platforms.winforms  # noqa: F401

            return True
        except Exception:
            return False


def _run_browser_fallback(httpd, url: str) -> None:
    """没有 WebView2：浏览器 + 托盘。"""
    import pystray
    from PIL import Image

    webbrowser.open(url)

    ico_path = _resource_base() / "app" / "assets" / "app.ico"
    try:
        image = Image.open(ico_path)
    except Exception:
        image = Image.new("RGB", (64, 64), (247, 178, 103))

    def on_quit(icon, item) -> None:  # noqa: ANN001
        httpd.shutdown()
        icon.stop()
        import os

        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("打开界面", lambda *_: webbrowser.open(url), default=True),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("miao_scan", image, APP_TITLE, menu)
    icon.run()


def _ensure_config() -> str | None:
    """首次运行：在程序旁边生成 config.yaml，返回新 token（用于提示）。

    没有这一步，冻结后的 exe 读不到配置就会退回空 token，被校验拦下——
    「启动失败：server.token 至少 16 位」就是这么来的。
    """
    cfg = _app_base() / "config.yaml"
    if cfg.exists():
        return None
    import secrets

    import run

    token = secrets.token_urlsafe(24)
    cfg.write_text(run.CONFIG_TEMPLATE.replace("{token}", token), encoding="utf-8")
    return token


def _say(msg: str) -> None:
    """windowed 模式下 sys.stdout 是 None，print 会抛异常，必须兜住。"""
    try:
        if sys.stdout is not None:
            print(msg, flush=True)
    except Exception:
        pass


def main() -> int:
    if not _single_instance():
        _alert(f"{APP_TITLE} 已经在运行了。\n请在任务栏或托盘找喵喵图标。")
        return 0

    _patch_paths()

    from scan.log import setup_logging

    import run  # noqa: E402  延迟到路径补丁之后

    token = _ensure_config()
    try:
        config, service = run._boot()
    except Exception as exc:  # 配置坏了也要给人看得懂的提示
        _alert(f"启动失败：{exc}")
        return 1
    setup_logging(config.log_dir)

    if token:
        _alert(
            "首次启动，已在程序目录生成 config.yaml。\n\n"
            f"手机端 token：{token}\n\n"
            "把这个 token 填到手机 App 里，用于拉取扫描任务。\n"
            "（文件里也能看到，别外传）"
        )

    httpd = _start_background(config, service)
    port = config.server.port
    url = f"http://127.0.0.1:{port}/"
    _say(f"{APP_TITLE} 已启动：{url}")

    try:
        _run_webview(httpd, url)
    except Exception:
        # 不吞异常：要能从日志看出为什么退回了浏览器模式
        from scan.log import get_logger

        get_logger("scan.desktop").exception("内嵌窗口启动失败，退回浏览器模式")
        try:
            _run_browser_fallback(httpd, url)
        except Exception:
            import traceback

            traceback.print_exc()
            _say(traceback.format_exc())
            _alert("界面启动失败，但服务还在 8766 端口。详情见 data/logs/scan.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
