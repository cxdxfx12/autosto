"""截一张程序主窗口（给改动做视觉验收，不进分发包）。

    .venv/Scripts/python.exe tools/shot_window.py docs/xxx.png [宽] [高]

为什么不用 PowerShell：本机策略禁止 Add-Type 编译 C#，而窗口置顶/取矩形
只需要几个 user32 调用，ctypes 足够。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

TITLE = "运单自动采集自动揽收助手"

user32 = ctypes.windll.user32


def find_window(title: str) -> int:
    return int(user32.FindWindowW(None, title))


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/shot.png")
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 1180
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 900

    hwnd = find_window(TITLE)
    if not hwnd:
        print("没找到窗口：", TITLE)
        return 1

    SWP_NOZORDER = 0x0004
    user32.SetWindowPos(hwnd, 0, 40, 20, width, height, SWP_NOZORDER)
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.0)

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    box = (rect.left, rect.top, rect.right, rect.bottom)
    out.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(bbox=box, all_screens=True).save(out)
    print("已保存", out, box)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
