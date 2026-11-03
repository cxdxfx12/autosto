"""把「C++ 外壳 + Chromium 内核 + Python 后端」组装成一个可分发目录。

    .venv/Scripts/python.exe tools/package_shell.py

产出：
    dist/运单自动采集自动揽收助手/
        运单自动采集自动揽收助手.exe     ← C++ 外壳（用户双击这个）
        libcef.dll / *.pak / locales/     ← 内置 Chromium 内核（不需要装任何浏览器）
        core\\miao-core.exe               ← Python 后端（微信采集/Excel/数据库/接口）

首次运行会在 exe 旁生成 config.yaml 与 data/ 目录。
"""

from __future__ import annotations

import shutil
import time
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "运单自动采集自动揽收助手"
SHELL_DIR = ROOT / "build" / "shell"
CORE_DIST = ROOT / "build" / "core_dist"
DIST = ROOT / "dist" / APP_NAME

CORE_CMD = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--windowed",
    "--name", "miao-core",
    "--add-data", f"{ROOT / 'app' / 'web'}{';'}app/web",
    "--add-data", f"{ROOT / 'app' / 'assets'}{';'}app/assets",
    # 手机监听/揽收那套脚本以源码形式随包（不是 exe）：现场排障常改它们，
    # 打进去后由 `miao-core.exe --phone-watch / --phone-run` 调用，见 core_main._run_tool。
    "--add-data", f"{ROOT / 'tools'}{';'}tools",
    "--hidden-import", "uiautomation",
    "--hidden-import", "comtypes",
    "--hidden-import", "openpyxl",
    "--collect-submodules", "comtypes",
    "--distpath", str(CORE_DIST),
    "--workpath", str(ROOT / "build" / "core_work"),
    "--specpath", str(ROOT / "build"),
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "numpy",
    "--exclude-module", "pandas",
    "--exclude-module", "webview",
    str(ROOT / "core_main.py"),
]

# 手机驱动要用的 adb（platform-tools 整包 17MB，实际只需要这三个文件）
ADB_SRC = ROOT / "vendor" / "platform-tools"
ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")


def _archive_dir(path: Path, tag: str) -> None:
    """老目录改名归档，避免批量删除触发安全策略拦截。"""
    if not path.exists():
        return
    trash = ROOT / "build" / f"_old_{tag}_{time.strftime('%H%M%S')}"
    if trash.exists():
        return
    shutil.move(str(path), str(trash))
    print(f"旧 {tag} 已归档到 {trash.name}")


def build_core() -> Path:
    print("打包 Python 后端（PyInstaller）...")
    # PyInstaller 每次会清空 dist/work 目录，先挪走，别让它批量删
    _archive_dir(CORE_DIST / "miao-core", "core")
    _archive_dir(ROOT / "build" / "core_work", "work")
    code = subprocess.call(CORE_CMD, cwd=ROOT)
    if code != 0:
        raise SystemExit("后端打包失败")
    exe = CORE_DIST / "miao-core" / "miao-core.exe"
    if not exe.exists():
        raise SystemExit(f"没找到 {exe}")
    return exe


def _archive_old_dist() -> Path | None:
    """旧产物移到 build/_old_dist_*，不做批量删除（安全策略会拦）。"""
    if not DIST.exists():
        return None
    stamp = time.strftime("%H%M%S")
    trash = ROOT / "build" / f"_old_dist_{stamp}"
    if trash.exists():
        return None
    shutil.move(str(DIST), str(trash))
    print(f"旧产物已归档到 {trash.name}（可以手动删）")
    return trash


# 重新打包时必须从旧产物里搬回来的运行期文件：
#   config.yaml → token、采集群、采集间隔、服务开关
#   data/       → 队列数据库（已入库单号、去重指纹）
#   inbox/      → 用户丢进去待扫的表格
# 不搬的话，每更新一次版本，客户的群名和待扫队列就被清空一次 ——
# 表现就是「开了服务却没采到」，非常容易踩。
RUNTIME_KEEP = ("config.yaml", "data", "inbox")
# Chromium 缓存几百 MB，且随时可重建，不往回搬
RUNTIME_SKIP = {"cef_cache"}


def _restore_runtime(archived: Path | None) -> None:
    if archived is None:
        return
    for name in RUNTIME_KEEP:
        src = archived / name
        if not src.exists():
            continue
        dst = DIST / name
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                if item.name in RUNTIME_SKIP:
                    continue
                target = dst / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
        else:
            shutil.copy2(src, dst)
        print(f"已沿用旧配置/数据：{name}")


def _bundle_adb() -> None:
    """把 adb 放到程序根 vendor/platform-tools —— 程序启动时会自己拉起手机监听。"""
    missing = [n for n in ADB_FILES if not (ADB_SRC / n).exists()]
    if missing:
        raise SystemExit(f"缺 adb 文件：{missing}（先放好 vendor/platform-tools）")
    dst = DIST / "vendor" / "platform-tools"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ADB_FILES:
        shutil.copy2(ADB_SRC / name, dst / name)


def assemble(core_exe: Path) -> None:
    shell_exe = SHELL_DIR / f"{APP_NAME}.exe"
    if not shell_exe.exists():
        raise SystemExit("还没有编译外壳，先跑：python tools/build_shell.py")

    archived = _archive_old_dist()
    DIST.mkdir(parents=True, exist_ok=True)

    print("拷贝外壳与 Chromium 内核 ...")
    # 排除编译产物与运行期数据：data/（缓存、日志）应首次运行时现生成，
    # 不带出去 —— 否则会把调试机的 cookie/session 一起拷给客户。
    skip_dirs = {"data", "wrapper_obj", "wrapper_objs"}
    skip_files = {"debug.log", "bootstrap.exe", "bootstrapc.exe"}
    for item in SHELL_DIR.iterdir():
        if item.name.endswith((".obj", ".res", ".lib", ".rsp", ".ico")) or item.name.startswith("."):
            continue
        if item.name in skip_dirs or item.name in skip_files:
            continue
        if item.is_dir():
            shutil.copytree(item, DIST / item.name)
        else:
            shutil.copy2(item, DIST / item.name)

    print("拷贝 Python 后端 ...")
    shutil.copytree(CORE_DIST / "miao-core", DIST / "core")

    print("拷贝 adb ...")
    _bundle_adb()

    print("沿用旧的配置与队列数据 ...")
    _restore_runtime(archived)

    total = sum(p.stat().st_size for p in DIST.rglob("*") if p.is_file()) / 1048576
    print()
    print(f"完成：{DIST}")
    print(f"整包 {total:.0f} MB")
    print(f"双击运行：{DIST / (APP_NAME + '.exe')}")


def main() -> int:
    if not SHELL_DIR.exists():
        raise SystemExit("还没有 build/shell，先跑 python tools/build_shell.py")
    core_exe = build_core()
    assemble(core_exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
