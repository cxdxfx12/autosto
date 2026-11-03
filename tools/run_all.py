"""一条命令托管「安装版 + 常驻监听」，用于在无人值守时保持运行。

    .venv/Scripts/python.exe tools/run_all.py [秒数，默认 0 = 一直跑]

做三件事：
  1) 拉起 e:\\运单自动采集自动揽收助手\\运单自动采集自动揽收助手.exe（已在跑就复用）
  2) 子进程起 tools/phone_watch.py 常驻监听 —— **仅当 config.yaml 里
     phone.auto_watch=false 时**才起；1.0.8 起手机监听由程序自己托管，
     这里再起一份就是双监听抢手机（有单实例锁兜着，但没必要）
  3) 循环看护：任一方退出就重启（外壳最多重启 3 次，防崩溃循环刷屏）

Ctrl+C 或父进程结束时会一并收掉这两个子进程。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = Path("e:/运单自动采集自动揽收助手/运单自动采集自动揽收助手.exe")
LOG = ROOT / "tmp" / "run_all.log"

MAX_APP_RESTART = 3


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def port_up(port: int = 8766) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_app() -> subprocess.Popen | None:
    if port_up():
        log("后端已在运行，复用现有实例")
        return None
    if not EXE.exists():
        log(f"！找不到 {EXE}")
        return None
    p = subprocess.Popen([str(EXE)], cwd=str(EXE.parent))
    log(f"启动安装版，外壳 PID {p.pid}")
    for _ in range(45):
        time.sleep(2)
        if port_up():
            log("后端已就绪")
            break
    else:
        log("！90s 内后端未监听")
    return p


def _auto_watch_enabled() -> bool:
    """程序自己会拉起手机监听（config.yaml 的 phone.auto_watch）。

    1.0.8 起这件事由 core 进程负责 —— 这个脚本再起一份就是双监听抢手机。
    只有在配置里显式关掉了自启的时候，才由这里兜底托管。
    """
    cfg = ROOT / "config.yaml"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return True
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("auto_watch:"):
            return "false" not in s.lower()
    return True


def start_watch() -> subprocess.Popen | None:
    if _auto_watch_enabled():
        log("程序自带手机监听（phone.auto_watch=true），不再单独托管")
        return None
    cmd = [sys.executable, str(ROOT / "tools" / "phone_watch.py"), "--interval", "10"]
    p = subprocess.Popen(cmd, cwd=str(ROOT))
    log(f"启动常驻监听，PID {p.pid}")
    return p


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    log("=" * 46)
    log("run_all 启动")

    app = start_app()
    watch = start_watch()
    app_restarts = 0
    t0 = time.time()

    try:
        while True:
            time.sleep(10)
            if limit and time.time() - t0 > limit:
                log(f"到达 {limit}s，退出")
                break

            if watch is not None and watch.poll() is not None:
                log(f"常驻监听退出（码 {watch.returncode}），重启")
                watch = start_watch()

            if app is not None and app.poll() is not None:
                code = app.returncode
                app_restarts += 1
                log(f"！外壳退出（码 {code}），第 {app_restarts} 次重启")
                if app_restarts > MAX_APP_RESTART:
                    log("！重启次数过多，停止重试（请查 data/logs/shell.log 与事件查看器）")
                    app = None
                else:
                    app = start_app()
    except KeyboardInterrupt:
        log("收到 Ctrl+C")
    finally:
        for name, p in (("监听", watch), ("外壳", app)):
            if p is not None and p.poll() is None:
                p.terminate()
                log(f"已结束{name} {p.pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
