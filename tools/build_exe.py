"""一键打包成标准 Windows 程序。

    .venv/Scripts/python.exe tools/build_exe.py                  # 轻量版（用系统 WebView2）
    .venv/Scripts/python.exe tools/build_exe.py --with-browser   # 离线完整版（包内自带浏览器内核）

产物：dist/运单自动采集自动揽收助手/运单自动采集自动揽收助手.exe（同目录含 _internal 与 webview2）
数据（config.yaml / data/ / inbox/）在 exe 旁生成，升级只换文件不动数据。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "运单自动采集自动揽收助手"
DIST_APP = ROOT / "dist" / APP_NAME
BROWSER_SRC = ROOT / "vendor" / "webview2"

BASE_CMD = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    # 窗口程序：不弹黑色控制台。排障时改 --console 重打一份即可。
    "--windowed",
    "--name", APP_NAME,
    "--icon", str(ROOT / "app" / "assets" / "app.ico"),
    "--version-file", str(ROOT / "app" / "assets" / "version_info.txt"),
    # 页面静态资源 + 图标
    "--add-data", f"{ROOT / 'app' / 'web'}{';'}app/web",
    "--add-data", f"{ROOT / 'app' / 'assets'}{';'}app/assets",
    # 隐式依赖：scan 里是函数内延迟 import，PyInstaller 静态分析可能漏
    "--hidden-import", "uiautomation",
    "--hidden-import", "comtypes",
    "--hidden-import", "openpyxl",
    "--hidden-import", "pystray",
    "--hidden-import", "PIL._tkinter_finder",
    "--collect-all", "webview",
    # 内嵌窗口靠 pythonnet 承载 .NET/WebView2，不显式收进来运行时会静默失败
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--hidden-import", "clr",
    "--hidden-import", "webview.platforms.winforms",
    "--hidden-import", "webview.platforms.edgechromium",
    "--collect-submodules", "comtypes",
    # 排除用不到的大块头
    "--exclude-module", "tkinter.test",
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy",
    "--exclude-module", "pandas",
    "--exclude-module", "PySide6",
    "--exclude-module", "PyQt5",
    str(ROOT / "desktop.py"),
]


def _kill_running() -> None:
    """dist 里的 exe 还在跑时，文件被占用会删不掉、也覆盖不了。"""
    exe = DIST_APP / f"{APP_NAME}.exe"
    if exe.exists():
        subprocess.run(
            ["taskkill", "/F", "/IM", f"{APP_NAME}.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def _bundle_browser() -> None:
    """把 vendor/webview2 原样拷到 exe 旁边，程序会优先用它。"""
    if not (BROWSER_SRC / "msedgewebview2.exe").exists():
        raise SystemExit(
            "没有找到 vendor/webview2，先跑一次：\n"
            "  .venv/Scripts/python.exe tools/fetch_webview2.py"
        )
    dst = DIST_APP / "webview2"
    if dst.exists():
        shutil.rmtree(dst)
    print("拷贝浏览器内核 ...（约 500MB，稍等）")
    shutil.copytree(BROWSER_SRC, dst)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-browser", action="store_true", help="把 WebView2 固定版内核打进包内")
    args = ap.parse_args()

    _kill_running()
    code = subprocess.call(BASE_CMD, cwd=ROOT)
    if code != 0:
        print("打包失败，退出码", code)
        return code

    out = DIST_APP / f"{APP_NAME}.exe"
    if not out.exists():
        # 兜底：按名字找一遍，避免编码/布局差异造成误报
        found = [p for p in (ROOT / "dist").rglob("*.exe") if APP_NAME in p.name]
        out = found[0] if found else out

    if args.with_browser:
        _bundle_browser()

    print()
    if out.exists():
        total = sum(p.stat().st_size for p in DIST_APP.rglob("*") if p.is_file()) / 1048576
        print(f"打包完成：{out}")
        print(f"exe：{out.stat().st_size / 1024 / 1024:.1f} MB    整包：{total:.0f} MB")
        if not args.with_browser:
            print("（轻量版：界面用系统 WebView2 运行时；要离线的请加 --with-browser）")
    else:
        print("警告：未找到预期的 exe，请检查 dist 目录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
