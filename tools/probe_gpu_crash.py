"""验证 GPU 崩溃是否由硬件加速引起，并确认软件渲染能稳住。

用法：
    .venv/Scripts/python.exe tools/probe_gpu_crash.py [观测秒数]

逻辑：带 MIAO_DISABLE_GPU=1 启动安装版，每秒探一次端口，跑够时间就报告，
并统计 cef.log 里是否还出现 GPU 进程崩溃。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

EXE = Path("e:/运单自动采集自动揽收助手/运单自动采集自动揽收助手.exe")
CEF_LOG = Path("e:/运单自动采集自动揽收助手/data/logs/cef.log")


def alive(port: int = 8766) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def cef_gpu_crashes_mark() -> str:
    """返回 cef.log 当前末尾标记，稍后用来判断这段时间有没有新增崩溃。"""
    if not CEF_LOG.exists():
        return ""
    return str(CEF_LOG.stat().st_size)


def cef_text_from(mark: str) -> str:
    if not CEF_LOG.exists():
        return ""
    data = CEF_LOG.read_bytes()
    return data[len(mark):].decode("utf-8", "ignore")


def main() -> None:
    keep = int(sys.argv[1]) if len(sys.argv) > 1 else 180

    if alive():
        raise SystemExit("8766 已在监听，先关掉正在跑的程序再测")

    env = dict(os.environ)
    env["MIAO_DISABLE_GPU"] = "1"
    print(f"带 MIAO_DISABLE_GPU=1 启动：{EXE}")
    proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env)
    print(f"外壳 PID {proc.pid}")

    mark = cef_gpu_crashes_mark()
    t0 = time.time()
    died_at = None
    while time.time() - t0 < keep:
        time.sleep(5)
        el = int(time.time() - t0)
        up = alive()
        code = proc.poll()
        print(f"{el:>4}s  端口={'OK' if up else '无'}  退出码={code}", flush=True)
        if code is not None:
            died_at = el
            break

    tail = cef_text_from(mark)
    gpu_crash = "GPU process exited unexpectedly" in tail
    if died_at is None:
        print(f"\n✓ 运行 {keep}s 未崩溃")
    else:
        print(f"\n✗ 第 {died_at}s 退出，退出码 {code}")
    print(f"  期间 GPU 进程崩溃记录：{'有' if gpu_crash else '无'}")

    if proc.poll() is None:
        print("（进程仍在跑，测试结束会保留，需要手动关）")


if __name__ == "__main__":
    main()
