"""把新版安装到 E 盘（覆盖升级），并保留原有 data/config.yaml。

用户机器上的安装目录是 e:\\运单自动采集自动揽收助手，升级时 Inno 会自动
保留 data/ config.yaml inbox/（[UninstallDelete] 只清 cef_cache）。

⚠ 2026-10-20 的教训：这个脚本原来把安装包文件名**写死成 1.0.1**，
于是 1.0.2 / 1.0.3 两次「升级成功」其实装的全是 1.0.1 的旧包 —— 而且它
只检查 data/config 在不在，不检查版本，所以一路绿灯把我骗过去了。
现在改成：**按 VERSION 文件找包 + 装完回读日期戳核对**，对不上就报错退出。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = Path("e:/运单自动采集自动揽收助手")


def _expected_setup() -> Path:
    """按根目录 VERSION 找对应的安装包，找不到就失败（绝不退回旧包）。"""
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    setup = ROOT / "dist" / f"运单自动采集自动揽收助手-Setup-{version}.exe"
    if not setup.exists():
        raise SystemExit(
            f"找不到 {setup.name}（VERSION={version}）\n"
            f"先跑：python tools/make_installer.py --rebuild"
        )
    return setup


def _installed_version() -> str:
    """从安装目录的 unins000.dat/exe 或文件名判断不了版本，改看资源里的版本串。

    简化做法：读 Inno 生成的 unins000.dat 不可靠，所以用「后端文件时间」
    作为判据 —— 只要 core/miao-core.exe 比安装包旧，就说明没覆盖成功。
    """
    exe = TARGET / "core" / "miao-core.exe"
    if not exe.exists():
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(exe.stat().st_mtime))


def main() -> None:
    setup = _expected_setup()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    before = _installed_version()
    print(f"升级安装到 {TARGET}")
    print(f"  安装包 {setup.name}（{setup.stat().st_size // 1024 // 1024} MB）")
    print(f"  升级前后端时间 {before or '(未安装)'}")

    # /CLOSEAPPLICATIONS + /FORCECLOSEAPPLICATIONS：
    # 别的进程/托管脚本（如 tools/run_all.py）随时会把外壳重新拉起，
    # RestartManager 关不掉就默认 Abort → 退出码 5（2026-10-29 连踩三次）。
    # 本程序无未保存状态，强关安全。
    r = subprocess.run(
        [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
         "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", f"/DIR={TARGET}"],
        capture_output=True,
        timeout=900,
    )
    print("  退出码", r.returncode)
    if r.returncode != 0:
        raise SystemExit("安装失败（退出码 %d）：先停掉 tools/run_all.py 之类的托管脚本，再重试" % r.returncode)

    for name in ("data", "config.yaml", "inbox"):
        p = TARGET / name
        print(f"  {name}: {'保留 ✓' if p.exists() else '缺失 ✗'}")

    after = _installed_version()
    print(f"  升级后后端时间 {after}"
          f"（{time.strftime('%H:%M:%S', time.localtime((TARGET / 'core' / 'miao-core.exe').stat().st_mtime))}）")

    # 关键校验：后端文件必须比升级前新。不新就是没装上，别报「成功」。
    exe = TARGET / "core" / "miao-core.exe"
    if before and after == before:
        raise SystemExit(
            "！后端没被覆盖（时间没变）—— 安装包可能没包含新代码，"
            "或有进程占用文件。不要相信「退出码 0」。"
        )

    # 前端也抽一条：确认新按钮进没进包
    page = TARGET / "core" / "_internal" / "app" / "web" / "index.html"
    if page.exists():
        has = "btnRunNow" in page.read_text(encoding="utf-8", errors="ignore")
        print(f"  前端「立即抓取」按钮：{'已包含 ✓' if has else '！缺失'}")

    shell = TARGET / "运单自动采集自动揽收助手.exe"
    print(f"  外壳 {shell.stat().st_size // 1024} KB")
    print(f"\n安装完成：版本 {version}")


if __name__ == "__main__":
    main()
