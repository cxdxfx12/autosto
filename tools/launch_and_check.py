"""启动安装版 + 验证接口，全程在同一个进程里跑，避免被外部清理掉。

用法：
    .venv/Scripts/python.exe tools/launch_and_check.py [保持秒数]

不传秒数 = 验证完就退出（自测用）；传了数字 = 保持那么久（给用户留窗口）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

EXE_CANDIDATES = [
    Path("e:/运单自动采集自动揽收助手/运单自动采集自动揽收助手.exe"),
    Path(__file__).resolve().parent.parent / "dist" / "运单自动采集自动揽收助手" / "运单自动采集自动揽收助手.exe",
]


def listening(port: int = 8766) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    keep = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    exe = next((p for p in EXE_CANDIDATES if p.exists()), None)
    if exe is None:
        raise SystemExit("找不到安装版 exe")
    print(f"启动：{exe}")

    if listening():
        print("8766 已在监听，跳过启动")
        proc = None
    else:
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        print(f"外壳 PID {proc.pid}")
        for i in range(45):
            time.sleep(2)
            if listening():
                print(f"{(i + 1) * 2}s 后端已就绪")
                break
        else:
            raise SystemExit("90s 内后端未监听")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import phone

    tok = phone._token()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request("http://127.0.0.1:8766/api/stats", headers={"X-Token": tok or ""})
    with opener.open(req, timeout=10) as resp:
        d = json.loads(resp.read().decode())

    print("=" * 50)
    print("服务运行:", d["service"]["running"])
    print("采集群  :", d["service"]["group_name"])
    print("自动模式:", d["service"]["auto_mode"])
    print("今日    :", d["today"]["ok"], "ok / pending", d["today"]["pending"])
    print("手机心跳:", d["phone"])
    print("微信    :", d["wechat"]["state"])
    print("=" * 50)

    if keep and proc is not None:
        print(f"保持运行 {keep}s（期间不要关这个窗口）…")
        t0 = time.time()
        while time.time() - t0 < keep:
            time.sleep(5)
            if proc.poll() is not None:
                print("！外壳提前退出")
                break
        print("保持结束")


if __name__ == "__main__":
    main()
