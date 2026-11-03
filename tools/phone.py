"""真机 adb 驱动：dump / 找控件 / 点 / 灌单号 / 等结果。

用法（所有子命令都支持 --serial 指定设备，不指定则自动挑第一台真机）：
    python tools/phone.py devices
    python tools/phone.py dump                 # 打印当前界面所有带文字的控件
    python tools/phone.py find 确定            # 查控件坐标
    python tools/phone.py tap-text 确定        # 按文字点击
    python tools/phone.py tap 540 1600
    python tools/phone.py type "SF123,SF456"   # 往焦点输入框灌文本
    python tools/phone.py clip "SF123,SF456"   # 写系统剪贴板（需 clipper/adbkeyboard）
    python tools/phone.py shot tmp/phone.png
    python tools/phone.py wait 揽收成功 --timeout 60

设计说明：
- 坐标全部用真机真实像素（dump 的 bounds），不要用截图缩放后的坐标。
- 长文本（500 单号 ≈ 8KB）优先走 ADBKeyboard 广播，一次提交；没有则退回 input text 分片。
- Git Bash 会把 /sdcard 转成 Windows 路径，脚本内部统一加 MSYS_NO_PATHCONV 环境，避免踩坑。
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

# 程序根：源码态是仓库根；被打进安装包后（tools 在 _internal 里，adb 在程序根
# 的 vendor 下）由 scan.phone_supervisor 通过 MIAO_ROOT 指过来。
_ENV_ROOT = os.environ.get("MIAO_ROOT", "").strip()
ROOT = Path(_ENV_ROOT).resolve() if _ENV_ROOT else Path(__file__).resolve().parent.parent
ADB = ROOT / "vendor" / "platform-tools" / "adb.exe"
if not ADB.exists():
    ADB = ROOT / "platform-tools" / "adb.exe"
if not ADB.exists():
    ADB = Path("adb")

ENV = dict(os.environ, MSYS_NO_PATHCONV="1")

# 常见 adb 输入法包名，装了就用广播一次性灌文本（比 input text 快几十倍）
ADBKB = "com.android.adbkeyboard"
CLIPPER = "clipper.android"  # majido/clipper

# 后端地址：PC 端脚本每次操作前打心跳，界面「手机」卡才不会显示失联
API_BASE = "http://127.0.0.1:8766"


def _token() -> str:
    """抓 server.token（避免依赖 pyyaml）。

    按「谁在真跑」排序：环境变量 → 已监听的 8766 到底是哪个目录的进程 →
    源码目录 → dist 绿色目录 → E 盘安装版。之前只认前两个，装了安装版后
    一直拿开发库的旧 token，接口全 401。
    """
    env = os.environ.get("STO_SERVER_TOKEN", "").strip()
    if env:
        return env

    def _from(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        m = re.search(r"^\s*token:\s*[\"']?([A-Za-z0-9_\-]+)", text, re.M)
        return m.group(1) if m else ""

    for path in _config_candidates():
        tok = _from(path)
        if tok:
            return tok
    return ""


def _config_candidates() -> list[Path]:
    """候选 config.yaml：优先当前后端进程实际使用的那个。"""
    out: list[Path] = []
    exe = _listening_core_dir()
    if exe:
        out.append(exe / "config.yaml")
    out.append(ROOT / "config.yaml")
    out.append(ROOT / "dist" / "运单自动采集自动揽收助手" / "config.yaml")
    for drive in ("E", "D", "C"):
        out.append(Path(f"{drive}:/运单自动采集自动揽收助手/config.yaml"))
    return out


def _listening_core_dir() -> Path | None:
    """8766 端口被哪个 miao-core 占着，返回它的上级（程序根）目录。

    实现说明：本机没有 wmic（Win11 已移除），netstat 输出是 GBK，
    所以走 ctypes 的 QueryFullProcessImageNameW 拿 exe 路径，最稳。
    """
    key = f":{API_BASE.rsplit(':', 1)[-1]}"
    try:
        p = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=8, env=ENV,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        text = p.stdout.decode("gbk", "ignore")
    except Exception:
        return None
    pids: list[str] = []
    for line in text.splitlines():
        if key in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.append(parts[-1])
    if not pids:
        return None
    exe = _process_exe(int(pids[0]))
    if not exe:
        return None
    core_dir = exe.parent
    return core_dir.parent if core_dir.name.lower() == "core" else core_dir


def _process_exe(pid: int) -> Path | None:
    """用 QueryFullProcessImageNameW 取进程 exe 全路径（PROCESS_QUERY_LIMITED_INFORMATION）。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return Path(buf.value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def heartbeat(page: str = "adb", timeout: float = 3.0) -> bool:
    """打一个手机心跳（失败静默，不影响主流程）。"""
    key = _token()
    if not key:
        return False
    url = f"{API_BASE}/api/phone/heartbeat?k={key}&page={page}"
    try:
        import urllib.request

        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return getattr(resp, "status", 0) == 200
    except Exception:
        return False


def run(args: list[str], serial: str | None = None, timeout: int = 60) -> str:
    cmd = [str(ADB)]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    # CREATE_NO_WINDOW：adb.exe 是控制台程序，从窗口化进程里调它
    # 会每次弹黑框（1.1.2 现场反馈）。加了就静默。
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=ENV,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    out = p.stdout.decode("utf-8", "ignore")
    err = p.stderr.decode("utf-8", "ignore")
    return out + err


def shell(cmd: str, serial: str | None = None, timeout: int = 60) -> str:
    return run(["shell", cmd], serial=serial, timeout=timeout).strip()


def devices() -> list[tuple[str, str, str]]:
    """返回 [(serial, state, model)]"""
    out = run(["devices", "-l"])
    rows = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1]
        rows.append((serial, state, model))
    return rows


def pick(serial: str | None = None) -> str:
    rows = [r for r in devices() if r[1] == "device"]
    if serial:
        if any(r[0] == serial for r in rows):
            return serial
        raise SystemExit(f"设备 {serial} 不在线，当前：{[r[0] for r in rows]}")
    real = [r for r in rows if not r[0].startswith("emulator-")]
    pool = real or rows
    if not pool:
        raise SystemExit("没有在线设备：请插 USB 并在手机上允许「USB 调试」")
    return pool[0][0]


# ---------------- 界面读取 ----------------

NODE_RE = re.compile(r"<node[^>]*>")


def attr(node: str, key: str) -> str:
    m = re.search(key + r'="([^"]*)"', node)
    return m.group(1) if m else ""


def dump_nodes(serial: str) -> list[dict]:
    shell("uiautomator dump /sdcard/wb_ui.xml >/dev/null 2>&1", serial=serial)
    xml = run(["exec-out", "cat /sdcard/wb_ui.xml"], serial=serial, timeout=30)
    nodes = []
    for m in NODE_RE.finditer(xml):
        n = m.group(0)
        b = attr(n, "bounds")
        nums = re.findall(r"-?\d+", b)
        if len(nums) < 4:
            continue
        x1, y1, x2, y2 = map(int, nums[:4])
        nodes.append(
            {
                "text": attr(n, "text"),
                "desc": attr(n, "content-desc"),
                "rid": attr(n, "resource-id"),
                "cls": attr(n, "class"),
                "clickable": attr(n, "clickable") == "true",
                "bounds": (x1, y1, x2, y2),
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            }
        )
    return nodes


def visible(nodes: list[dict]) -> list[dict]:
    return [n for n in nodes if n["text"] or n["desc"]]


def find(nodes: list[dict], text: str, exact: bool = False) -> dict | None:
    for n in nodes:
        t, d = n["text"], n["desc"]
        if exact and (t == text or d == text):
            return n
        if not exact and (text in t or text in d):
            return n
    return None


# ---------------- 操作 ----------------

def tap(serial: str, x: int, y: int) -> None:
    shell(f"input tap {x} {y}", serial=serial)


def tap_text(serial: str, text: str, exact: bool = False, retries: int = 3) -> dict:
    for i in range(retries):
        n = find(dump_nodes(serial), text, exact)
        if n:
            tap(serial, *n["center"])
            return n
        time.sleep(1.2)
    raise SystemExit(f"界面上找不到「{text}」")


def installed(serial: str, pkg: str) -> bool:
    return pkg in shell(f"pm list packages {pkg}", serial=serial)


# ---------------- 把 App 调到前台 ----------------
# 申行者的 SxzHomeActivity 没有导出，adb shell 直接 am start 会被 SecurityException 拒，
# 只能走 LAUNCHER 意图（monkey）。实测冷启动到首页约 6 秒。

APP = "cn.sto.sxz.core.app"
HOME_TEXTS = ("揽件扫描", "保存去扫描")


def wake(serial: str) -> str:
    """点亮屏幕并尝试解除锁屏（有密码时解不开，只能靠人）。"""
    state = shell("dumpsys power", serial=serial, timeout=30)
    m = re.search(r"mWakefulness=(\w+)", state)
    if m and m.group(1) != "Awake":
        shell("input keyevent 224", serial=serial)  # KEYCODE_WAKEUP
        time.sleep(0.8)
    shell("wm dismiss-keyguard", serial=serial)
    time.sleep(0.4)
    return m.group(1) if m else "unknown"


def focused_pkg(serial: str) -> str:
    """当前焦点窗口属于哪个包（区分不出时返回空串）。"""
    out = shell("dumpsys window", serial=serial, timeout=30)
    m = re.search(r"mCurrentFocus=\S+ \S+ ([A-Za-z0-9_.]+)/", out)
    return m.group(1) if m else ""


def ensure_app(serial: str, timeout: int = 40, pkg: str = APP) -> str:
    """确保 App 在前台且首页可用。

    返回 already（本来就在）/ launched（本次拉起）/ failed（拉不起来，通常是有密码的锁屏，
    或 App 卡在登录页、升级弹窗）。
    """
    wake(serial)
    t0 = time.time()
    launched = False
    last_launch = 0.0
    back_presses = 0
    while time.time() - t0 < timeout:
        current = focused_pkg(serial)
        if current != pkg:
            # 4 秒内不重复发拉起，避免 monkey 把启动页反复顶掉
            if time.time() - last_launch > 4:
                shell(
                    f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1",
                    serial=serial,
                    timeout=60,
                )
                launched = True
                last_launch = time.time()
            time.sleep(2.5)
            continue
        nodes = dump_nodes(serial)
        if any(find(nodes, text) for text in HOME_TEXTS):
            return "launched" if launched else "already"
        # 已在 App 内的任意业务页（批量输入页/扫描页/业务弹窗）也算就绪：
        # 页面导航交给 reset_to_scan / ensure_batch_page，别在这里卡死。
        # 实测 2026-10-26：上一轮遗留的「温馨提示」弹窗 + 停在批量输入页时
        # 没有 HOME_TEXTS，原逻辑 40s 超时误报「锁屏有密码 / 停在登录页」。
        if nodes:
            return "launched" if launched else "already"
        # 「AI 快递员助手」引导等自绘弹窗：节点文本全空但 App 在前台，
        # 按一次返回即可关掉（实测 2026-10-20）。限量按，别把正常页面退掉。
        if not nodes and back_presses < 2:
            shell("input keyevent 4", serial=serial)
            back_presses += 1
            time.sleep(2)
            continue
        time.sleep(2)
    return "failed"


def front_texts(serial: str, limit: int = 8) -> list[str]:
    """排障用：当前界面上有哪些字（判断是不是卡在登录/弹窗）。"""
    nodes = dump_nodes(serial)
    return [n["text"].strip() for n in nodes if n["text"].strip()][:limit]


def set_clip(serial: str, text: str) -> str:
    """把文本写进手机剪贴板，返回实际用的通道。"""
    if installed(serial, CLIPPER):
        shell(f"am broadcast -a clipper.set -e text {shlex.quote(text)}", serial=serial)
        return "clipper"
    if installed(serial, ADBKB):
        shell(f"am broadcast -a ADB_INPUT_TEXT --es msg {shlex.quote(text)}", serial=serial)
        return "adbkeyboard"
    return "none"


def type_text(serial: str, text: str, chunk: int = 400) -> str:
    """往当前焦点输入框灌文本。优先 ADBKeyboard 广播，其次 input text 分片。"""
    if installed(serial, ADBKB):
        shell(f"am broadcast -a ADB_INPUT_TEXT --es msg {shlex.quote(text)}", serial=serial)
        return "adbkeyboard"
    safe = text.replace(" ", "%s")
    for i in range(0, len(safe), chunk):
        piece = safe[i : i + chunk]
        shell(f"input text {shlex.quote(piece)}", serial=serial, timeout=180)
        time.sleep(0.3)
    return "input"


def wait_text(serial: str, text: str, timeout: int = 60) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        n = find(dump_nodes(serial), text)
        if n:
            return n
        time.sleep(2)
    raise SystemExit(f"等待超时：{timeout}s 内没看到「{text}」")


def shot(serial: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    png = subprocess.run(
        [str(ADB), "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        env=ENV,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    path.write_bytes(png)


# ---------------- CLI ----------------

def main() -> None:
    ap = argparse.ArgumentParser(description="真机 adb 驱动")
    ap.add_argument("--serial", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices")
    sub.add_parser("app")
    sub.add_parser("dump")
    p = sub.add_parser("find")
    p.add_argument("text")
    p.add_argument("--exact", action="store_true")
    p = sub.add_parser("tap-text")
    p.add_argument("text")
    p.add_argument("--exact", action="store_true")
    p = sub.add_parser("tap")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p = sub.add_parser("type")
    p.add_argument("text")
    p = sub.add_parser("clip")
    p.add_argument("text")
    p = sub.add_parser("wait")
    p.add_argument("text")
    p.add_argument("--timeout", type=int, default=60)
    p = sub.add_parser("shot")
    p.add_argument("path")

    a = ap.parse_args()

    if a.cmd == "devices":
        rows = devices()
        if not rows:
            print("（无设备）插 USB → 手机设置里开「开发者选项 / USB 调试」→ 手机上点「允许」")
        for s, st, model in rows:
            kind = "模拟器" if s.startswith("emulator-") else "真机"
            print(f"{s:24} {st:10} {kind} {model}")
        return

    serial = pick(a.serial)
    print(f"[device] {serial}", file=sys.stderr)

    if a.cmd == "app":
        state = ensure_app(serial)
        print(f"{state}  focus={focused_pkg(serial) or '(未知)'}")
        if state == "failed":
            print("当前界面文字：" + " | ".join(front_texts(serial)))
            print("多半是锁屏有密码、停在登录页，或 App 弹了升级/公告弹窗")
        return

    if a.cmd == "dump":
        for n in visible(dump_nodes(serial)):
            print(f"{n['center']!s:>12}  {n['text'][:60]!r}  {('desc=' + n['desc'][:30]) if n['desc'] else ''}")
    elif a.cmd == "find":
        n = find(dump_nodes(serial), a.text, a.exact)
        print(n["center"] if n else "未找到")
    elif a.cmd == "tap-text":
        n = tap_text(serial, a.text, a.exact)
        print(f"已点击 {n['center']} 「{n['text'] or n['desc']}」")
    elif a.cmd == "tap":
        tap(serial, a.x, a.y)
        print(f"已点击 ({a.x},{a.y})")
    elif a.cmd == "type":
        print(f"已输入 {len(a.text)} 字符（{type_text(serial, a.text)}）")
    elif a.cmd == "clip":
        ch = set_clip(serial, a.text)
        print(f"剪贴板写入 {len(a.text)} 字符（通道 {ch}）")
    elif a.cmd == "wait":
        n = wait_text(serial, a.text, a.timeout)
        print(f"出现 {n['center']} 「{n['text']}」")
    elif a.cmd == "shot":
        shot(serial, Path(a.path))
        print(f"截图 {a.path}")


if __name__ == "__main__":
    main()
