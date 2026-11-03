"""微信 4.x 只读探针：把当前聊天窗口的消息列表打出来。

**只读，不点击、不切换会话、不输入**。用于验证：
1. 本机微信是否向 UIA 暴露 `mmui::` 控件树；
2. 当前打开的群能不能读到正文；
3. 时间分隔条长什么样（采集器必须把它过滤掉）。

用法：
    .venv/Scripts/python.exe tools/probe_wechat.py
    .venv/Scripts/python.exe tools/probe_wechat.py --tail 20

已知故障态（ddwx-bridge 实测记录）：微信某些启动/托盘往返后，主窗口会变成
`Qt51514QWindowIcon` 自绘壳，屏幕上看得到但 UIA 子树是空的。
此时**完全退出微信再打开**即可恢复，不用改代码。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import uiautomation as uia
except ImportError:
    print("缺少依赖：pip install uiautomation comtypes")
    raise SystemExit(2)

WINDOW_CLASS = "mmui::MainWindow"
MESSAGE_LIST = "mmui::RecyclerListView"
TEXT_ITEM = "mmui::ChatTextItemView"
TIME_ITEM = "mmui::ChatItemView"
BUBBLE_ITEM = "mmui::ChatBubbleItemView"
TITLE_BAR = "mmui::ChatTitleBarChatRoomView"


def find_window() -> object | None:
    uia.SetGlobalSearchTimeout(3)
    wnd = uia.WindowControl(searchDepth=1, ClassName=WINDOW_CLASS)
    if wnd.Exists(2, 1):
        return wnd

    # 诊断：屏幕上有没有微信的 Qt 壳窗口（有它却读不到 mmui，就是已知故障态）
    shell = uia.WindowControl(searchDepth=1, RegexName=".*微信.*")
    if shell.Exists(1, 1):
        print("找到标题含『微信』的窗口，但类名不是 mmui::MainWindow：")
        print(f"  class={shell.ClassName!r} name={shell.Name!r} rect={shell.BoundingRectangle}")
        print("这通常意味着微信退化成了 Qt 自绘壳，UIA 子树为空。")
        print("处理：完全退出微信（托盘右键退出），重新打开后再试。")
    else:
        print("没有找到微信主窗口。请先打开微信并进入目标群聊。")
    return None


def chat_title(wnd) -> str:
    try:
        bar = wnd.Control(ClassName=TITLE_BAR, searchDepth=30)
        names = [c.Name for c in bar.GetChildren() if c.Name]
        return " / ".join(names) if names else ""
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tail", type=int, default=15, help="打印最近几条")
    args = parser.parse_args()

    wnd = find_window()
    if not wnd:
        return 1

    print(f"主窗口：class={wnd.ClassName!r} name={wnd.Name!r} rect={wnd.BoundingRectangle}")
    print(f"当前会话标题：{chat_title(wnd)!r}")

    try:
        msg_list = wnd.Control(ClassName=MESSAGE_LIST, searchDepth=30)
    except Exception:
        print("找不到消息列表 mmui::RecyclerListView —— 可能不在聊天页，或在搜索/通讯录页。")
        return 1

    items = msg_list.GetChildren()
    print(f"消息列表控件：{len(items)} 个子节点")
    print("-" * 78)
    for item in items[-args.tail:]:
        cls = item.ClassName
        name = (item.Name or "").replace("\n", "\\n")
        kind = {
            TEXT_ITEM: "正文",
            TIME_ITEM: "时间",
            BUBBLE_ITEM: "文件/图片",
        }.get(cls, "其他")
        print(f"[{kind:6}] {cls:28} {name[:90]}")
    print("-" * 78)
    print("判定：只要『正文』行里出现完整单号，就说明这条链路可行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
