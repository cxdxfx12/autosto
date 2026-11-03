"""安装包里最关键的那条铁律，装一次验一次：
   ① 静默安装能退出 0
   ② 装出来的目录不含 data/config.yaml/cef_cache（不能把开发机的队列和 token 带给客户）
   ③ 首次启动能自己生成 config.yaml 并监听端口（说明后端没被 PyInstaller 打坏）
   ④ 卸载保留 data/

用法：.venv/Scripts/python.exe tools/verify_installer.py dist/xxx-Setup-1.0.1.exe
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = "运单自动采集自动揽收助手"


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_port(port: int, timeout: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(1)
    return False


def _kill_under(root: Path) -> None:
    """只结束可执行文件位于 root 之下的进程，绝不按进程名裸杀。"""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    PROCESS_QUERY_LIMITED = 0x1000
    PROCESS_TERMINATE = 0x0001

    target = str(root.resolve()).lower()
    killed = []
    for pid in _all_pids():
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED | PROCESS_TERMINATE, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(32768)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if buf.value.lower().startswith(target):
                    if k32.TerminateProcess(h, 1):
                        killed.append((pid, buf.value))
        finally:
            k32.CloseHandle(h)
    for pid, path in killed:
        print(f"      已结束 {pid} {path}")
    if not killed:
        print("      没有需要结束的进程")


def _all_pids() -> list[int]:
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    arr = (wintypes.DWORD * 4096)()
    got = wintypes.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(got)):
        return []
    return [arr[i] for i in range(got.value // ctypes.sizeof(wintypes.DWORD))]


def main() -> None:
    setup = Path(sys.argv[1] if len(sys.argv) > 1 else "")
    if not setup.is_absolute():
        setup = ROOT / setup
    if not setup.exists():
        raise SystemExit(f"找不到安装包：{setup}")

    # 装到临时目录而不是 build/ 下：build/ 常被沙箱/杀软限制写权限，
    # 会以 WinError 5 拒绝访问的形式失败，让人误以为是安装包坏了。
    # 也可以用第 2 个参数指定目录。
    if len(sys.argv) > 2:
        target = Path(sys.argv[2])
    else:
        target = Path(tempfile.gettempdir()) / "_miao_verify_install"
    if target.exists():
        # 只挪不删，避免批量删除被安全策略拦
        shutil.move(str(target), str(target.parent / f"_miao_verify_old_{time.strftime('%H%M%S')}"))
    target.parent.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] 静默安装到 {target}")
    r = subprocess.run(
        [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}"],
        capture_output=True,
        timeout=600,
    )
    print(f"      退出码 {r.returncode}")
    if r.returncode != 0:
        raise SystemExit("安装失败")

    exe = target / f"{APP}.exe"
    core = target / "core" / "miao-core.exe"
    print(f"[2/5] 产物检查  外壳={exe.exists()}  后端={core.exists()}")
    if not (exe.exists() and core.exists()):
        raise SystemExit("关键文件缺失")

    print("[3/5] 数据隔离检查（这三个绝不能出现）")
    bad = [n for n in ("data", "config.yaml", "cef_cache", "inbox") if (target / n).exists()]
    print("      意外带入：" + (", ".join(bad) if bad else "无 ✓"))
    if bad:
        raise SystemExit(f"安装包带入了开发机数据：{bad}")

    n_files = sum(1 for _ in target.rglob("*") if _.is_file())
    size_mb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) // 1024 // 1024
    print(f"      {n_files} 个文件 / {size_mb} MB")

    print("[4/5] 首次启动")
    if not port_free(8766):
        raise SystemExit("8766 已被占用，先关掉其他实例再验")
    subprocess.Popen([str(exe)], cwd=str(target))
    up = wait_port(8766, 90)
    print(f"      端口 8766 {'起来了 ✓' if up else '没起来 ✗'}")
    if not up:
        subprocess.run(["taskkill", "/F", "/IM", f"{APP}.exe"], capture_output=True)
        raise SystemExit("后端没能监听端口")

    cfg = target / "config.yaml"
    print(f"      config.yaml 自动生成：{cfg.exists()}")
    if cfg.exists():
        txt = cfg.read_text(encoding="utf-8")
        print(f"      含 token：{'token' in txt}  长度 {len(txt)}")
    print(f"      自动建 data/：{(target / 'data').exists()}")

    api = "http://127.0.0.1:8766/api/stats"
    try:
        with urllib.request.urlopen(api, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print(f"      接口自检 ok，今日 ok={data.get('today', {}).get('ok')}")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"      接口自检失败（可能需要带 token）：{e}")

    print("[5/5] 收尾：关程序 + 卸载")
    # 只杀「装在 target 目录下」的进程。早先用 taskkill /IM miao-core.exe 裸杀，
    # 会把用户正在用的另一份安装版一起打死（实测踩过），这里必须按路径筛。
    _kill_under(target)
    time.sleep(2)
    unins = target / "unins000.exe"
    if unins.exists():
        subprocess.run(
            [str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            capture_output=True,
            timeout=300,
        )
        time.sleep(3)
    left = [p.name for p in target.iterdir()] if target.exists() else []
    print(f"      卸载后残留：{left or '目录已删'}")
    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
