"""手机侧安装包 / 脚本的分发小站（内网 8898）。

为什么单独起一个：APK 有 200MB，不想塞进业务服务的路由里；
手机上直接输一个地址就能下载安装包和脚本，不用连数据线。

    python tools/phone_serve.py            # 默认 8898
"""

from __future__ import annotations

import functools
import http.server
import socket
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "tools" / "phone_web"
APK = ROOT / "apk"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8898


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        # /apk/xxx.apk 走 apk 目录，其余走 phone_web
        clean = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        if clean.startswith("apk/"):
            return str(APK / clean[4:])
        if clean in ("", "/"):
            return str(WEB / "index.html")
        return str(WEB / clean)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.address_string()}] {fmt % args}", flush=True)


def main() -> None:
    socketserver.TCPServer.allow_reuse_address = True
    handler = functools.partial(Handler, directory=str(WEB))
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), handler) as srv:
        print(f"手机分发站：http://{lan_ip()}:{PORT}", flush=True)
        print(f"  APK : {APK}", flush=True)
        print(f"  脚本: {WEB}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
