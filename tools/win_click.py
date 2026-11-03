"""在程序窗口的「页面坐标」上点一下（视觉验收用，不进分发包）。

    .venv/Scripts/python.exe tools/win_click.py 1080 96

先把窗口挪到固定位置（和 shot_window.py 一致），再按页面左上角为原点点击，
这样截图量出来的坐标可以直接用。页面缩放不是 100% 时用 --scale 修正。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

TITLE = "运单自动采集自动揽收助手"
user32 = ctypes.windll.user32

SWP_NOZORDER = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def find_window(title: str) -> int:
    return int(user32.FindWindowW(None, title))


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: win_click.py <x> <y> [--scale 1.0] [--stay]")
        return 2
    x = float(sys.argv[1])
    y = float(sys.argv[2])
    scale = 1.0
    stay = "--stay" in sys.argv
    if "--scale" in sys.argv:
        scale = float(sys.argv[sys.argv.index("--scale") + 1])

    hwnd = find_window(TITLE)
    if not hwnd:
        print("没找到窗口：", TITLE)
        return 1

    if not stay:
        user32.SetWindowPos(hwnd, 0, 40, 20, 1180, 900, SWP_NOZORDER)
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)

    # 客户区左上角换算到屏幕坐标（排除标题栏/边框）
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    sx, sy = int(pt.x + x * scale), int(pt.y + y * scale)

    user32.SetCursorPos(sx, sy)
    time.sleep(0.15)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    print(f"clicked page({x:.0f},{y:.0f}) -> screen({sx},{sy})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
