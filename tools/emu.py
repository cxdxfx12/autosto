"""模拟器（MuMu 12）控制台：通过 adb 操作挂在模拟器里的申行者。

设计原则：
- 只用 adb，不碰 MuMu 自己的进程（沙箱策略会杀 MuMu 的 GUI，但 adb 是普通 TCP 连接）。
- 所有"找控件"都走 uiautomator dump，按文本/resource-id 定位，不硬编码坐标；
  申行者小版本更新后改文案也能快速适配。
- 单条命令可独立执行，方便人工排查：python tools/emu.py <子命令>

常用：
  python tools/emu.py status            # 连上了吗、当前在哪个页面
  python tools/emu.py shot out.png      # 截图
  python tools/emu.py dump              # 打印可点击控件（文本 + 中心点）
  python tools/emu.py tap-text "确定"    # 按文本点
  python tools/emu.py text "770000..."  # 给焦点输入框喂文本
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADB = ROOT / "vendor" / "platform-tools" / "adb.exe"
SERIAL = "127.0.0.1:16384"
PKG = "cn.sto.sxz.core.app"


def adb(*args: str, timeout: int = 60, binary: bool = False):
    """执行 adb。文本模式下强制 utf-8，避免 GBK 乱码把中文文案读坏。"""
    cmd = [str(ADB), "-s", SERIAL, *args]
    out = subprocess.run(cmd, capture_output=True, timeout=timeout,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    if binary:
        return out.stdout
    return out.stdout.decode("utf-8", "replace")


def ensure_connected(retries: int = 3) -> bool:
    for _ in range(retries):
        if SERIAL in adb("devices"):
            return True
        adb("connect", SERIAL)
        time.sleep(1)
    return SERIAL in adb("devices")


def top_activity() -> str:
    out = adb("shell", "dumpsys activity activities | grep topResumedActivity")
    m = re.search(r"u0 ([^ }]+)", out)
    return m.group(1) if m else ""


def dump_xml() -> str:
    """拿控件树。注意：dump 里会残留已经消失的弹窗层，判断页面以截图为准。"""
    adb("shell", "uiautomator dump /sdcard/ui.xml")
    return adb("exec-out", "cat /sdcard/ui.xml")


def nodes(xml: str) -> list[dict]:
    """把 dump 拆成 [{text, rid, cls, clickable, center}]。"""
    out = []
    for m in re.finditer(r"<node\b([^>]*)/?>", xml):
        attrs = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', m.group(1)))
        b = attrs.get("bounds", "")
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
        if not bm:
            continue
        x1, y1, x2, y2 = map(int, bm.groups())
        out.append(
            {
                "text": attrs.get("text", ""),
                "rid": attrs.get("resource-id", ""),
                "cls": attrs.get("class", ""),
                "desc": attrs.get("content-desc", ""),
                "clickable": attrs.get("clickable") == "true",
                "bounds": (x1, y1, x2, y2),
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            }
        )
    return out


def find(nodes_: list[dict], text: str = "", rid: str = "", exact: bool = False) -> dict | None:
    for n in nodes_:
        if rid and rid in n["rid"]:
            return n
        if text:
            label = n["text"] or n["desc"]
            if not label:
                continue
            if (label == text) if exact else (text in label):
                return n
    return None


def tap(x: int, y: int) -> None:
    adb("shell", f"input tap {x} {y}")


def tap_text(text: str, exact: bool = False, retries: int = 3) -> bool:
    for i in range(retries):
        n = find(nodes(dump_xml()), text=text, exact=exact)
        if n:
            tap(*n["center"])
            return True
        time.sleep(1)
    return False


def type_text(text: str) -> None:
    # 中文/符号走 input text 会丢字符，单号是纯数字字母，直接送没问题
    safe = text.replace(" ", "%s")
    adb("shell", f'input text "{safe}"')


def screenshot(path: str | Path) -> Path:
    p = Path(path)
    p.write_bytes(adb("exec-out", "screencap -p", binary=True))
    return p


def launch_app() -> None:
    adb("shell", f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]

    if cmd == "status":
        ok = ensure_connected()
        print("adb:", "在线" if ok else "离线")
        if not ok:
            return 2
        print("页面:", top_activity())
        return 0

    if cmd == "shot":
        out = rest[0] if rest else "docs/emu_now.png"
        ensure_connected()
        print("保存", screenshot(ROOT / out))
        return 0

    if cmd == "dump":
        ensure_connected()
        for n in nodes(dump_xml()):
            label = n["text"] or n["desc"]
            if label or n["rid"]:
                flag = "*" if n["clickable"] else " "
                print(f"{flag} {label[:34]:36s} {n['rid'][:40]:42s} {n['center']}")
        return 0

    if cmd == "tap-text":
        ensure_connected()
        print("找到并点击:", tap_text(rest[0], exact="--exact" in rest))
        return 0

    if cmd == "tap":
        ensure_connected()
        tap(int(rest[0]), int(rest[1]))
        return 0

    if cmd == "text":
        ensure_connected()
        type_text(rest[0])
        return 0

    if cmd == "launch":
        ensure_connected()
        launch_app()
        return 0

    print("未知子命令:", cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
