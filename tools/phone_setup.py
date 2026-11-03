"""真机一键部署：连上手机后装申行者、拉起、导出当前页控件。

用法：
    python tools/phone_setup.py            # 检查/安装/启动/导出
    python tools/phone_setup.py --dump     # 只导出当前页
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import phone  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKG = "cn.sto.sxz.core.app"
APK = ROOT / "sxz_app_release(1).apk"


def show(nodes: list[dict], limit: int = 60) -> None:
    rows = [n for n in nodes if (n.get("text") or n.get("desc")) and n.get("visible")]
    for n in rows[:limit]:
        print(
            f"{n.get('text')!r:40} | {n.get('desc')!r:24} | "
            f"{n.get('cls','').split('.')[-1]:16} | {n.get('bounds')}"
        )
    print(f"-- 共 {len(rows)} 个可读控件")


def main() -> None:
    serial = phone.pick()
    print(f"[设备] {serial}")
    print(phone.shell("getprop ro.product.model; getprop ro.build.version.release", serial=serial).strip())

    if "--dump" not in sys.argv:
        if phone.installed(serial, PKG):
            print(f"[安装] {PKG} 已安装")
        elif APK.exists():
            print(f"[安装] {APK.name} ({APK.stat().st_size // 1048576}MB) 正在安装…")
            print(phone.run(["install", "-r", "-g", str(APK)], serial=serial, timeout=600).strip()[-300:])
        else:
            print(f"[安装] 找不到 {APK}")
            return
        print("[启动] 拉起申行者")
        phone.shell(f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1", serial=serial)
        time.sleep(8)

    print("[页面] " + phone.shell(
        "dumpsys activity activities | grep -E 'topResumedActivity' | head -1", serial=serial
    ).strip())
    nodes = phone.dump_nodes(serial)
    print(f"[控件] {len(nodes)} 个节点")
    show(nodes)

    (ROOT / "tmp").mkdir(exist_ok=True)
    phone.shot(serial, ROOT / "tmp" / "phone_now.png")
    print("[截图] tmp/phone_now.png")


if __name__ == "__main__":
    main()
