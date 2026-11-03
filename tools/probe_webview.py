"""WebView2 渲染探针 v2：按窗口实际位置取色，直接报「渲染出画面没有」。

    python tools/probe_webview.py "<browser flags>" <输出.png>

跑 6 秒后自动截图、按窗口客户区中心取色并退出。
绿色渐变页面渲染成功 => 中心像素偏绿/橙；黑屏 => (0,0,0) 附近。
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

flags = sys.argv[1] if len(sys.argv) > 1 else ""
out = sys.argv[2] if len(sys.argv) > 2 else "docs/probe.png"

if flags:
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = flags

import webview  # noqa: E402
from PIL import ImageGrab  # noqa: E402

HTML = """<!doctype html><html><body style="margin:0;height:100vh;
background:linear-gradient(135deg,#f0742c 0%,#ffd9a8 100%);
display:flex;align-items:center;justify-content:center;font-family:'Microsoft YaHei',sans-serif">
<h1 style="color:#fff;font-size:44px">渲染测试 OK</h1></body></html>"""


def probe() -> None:
    w = webview.windows[0]
    loaded = w.events.loaded.wait(timeout=20)
    print(f"[probe] loaded={loaded}", flush=True)
    time.sleep(1)
    try:
        # 1) 让内核自己报告 DOM 状态：能拿到说明页面真的加载并渲染了
        if loaded:
            inner = w.evaluate_js(
                "JSON.stringify({t: document.title, bg: getComputedStyle(document.body).backgroundImage.slice(0,30), h1: (document.querySelector('h1')||{}).innerText})"
            )
            print(f"[probe] DOM = {inner}", flush=True)

        # 2) 屏幕上窗口中心取色：拿不到说明是「合成到屏幕」这一环断了
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, "probe")
        r = wintypes.RECT()
        u.GetClientRect(hwnd, ctypes.byref(r))
        pt = wintypes.POINT(r.right // 2, r.bottom // 2)
        u.ClientToScreen(hwnd, ctypes.byref(pt))
        im = ImageGrab.grab(all_screens=True)
        im.save(out)
        rgb = im.getpixel((pt.x, pt.y))
        ok = rgb[0] > 150 and rgb[2] < 180
        print(f"[probe] flags={flags!r} screen_center={rgb} 判定={'屏幕可见' if ok else '屏幕不可见'} -> {out}", flush=True)
    except Exception as exc:
        print(f"[probe] 出错 {exc}", flush=True)
    finally:
        os._exit(0)


webview.create_window("probe", html=HTML, width=760, height=460)
webview.start(probe, gui="edgechromium")
