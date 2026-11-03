"""ADBKeyboard 一次性注入单号（毫秒级），并可选点「确定」+ 自动收割弹窗。

用法：
    python tools/phone_inject.py --count 500              # 只注入，校验输入框
    python tools/phone_inject.py --count 500 --confirm    # 注入 + 点确定 + 收割弹窗
"""
from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import phone  # noqa: E402
import phone_paste as pp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ADBKB = "com.android.adbkeyboard"
# 弹窗里常见的确认词
OK_WORDS = ("确定", "知道了", "好的", "关闭", "我知道了", "继续")
# 异常提示关键词
BAD_WORDS = ("拦截", "超重", "重复", "已揽收", "已扫描", "失败", "异常", "不能", "不可", "超过", "限额")


def broadcast(serial: str, action: str, payload: str | None = None) -> str:
    cmd = f"am broadcast -a {action}"
    if payload is not None:
        cmd += f" --es msg {shlex.quote(payload)}"
    return phone.shell(cmd, serial=serial, timeout=120)


def edit_text(serial: str) -> str:
    n = pp.edit_node(phone.dump_nodes(serial))
    return n["text"] if n else ""


def inject(serial: str, text: str) -> None:
    n = pp.edit_node(phone.dump_nodes(serial))
    if not n:
        raise SystemExit("找不到输入框，先导航到批量输入页")
    phone.tap(serial, *n["center"])
    time.sleep(1.0)
    broadcast(serial, "ADB_CLEAR_TEXT")
    time.sleep(0.4)
    broadcast(serial, "ADB_INPUT_TEXT", text)
    time.sleep(1.5)


def harvest_popups(serial: str, rounds: int = 12) -> list[str]:
    """点确定后反复抓弹窗文本并关掉，返回异常描述列表。"""
    seen: list[str] = []
    quiet = 0
    for _ in range(rounds):
        ns = phone.dump_nodes(serial)
        texts = [n["text"].strip() for n in ns if n["text"].strip()]
        has_ok = any(t in OK_WORDS for t in texts)
        bad = [t for t in texts if any(w in t for w in BAD_WORDS)]
        # 弹窗特征：有确认词 + 有异常提示，且页面还停留在输入/列表页
        if has_ok and bad:
            desc = " / ".join(dict.fromkeys(bad))[:120]
            seen.append(desc)
            for w in OK_WORDS:
                n = phone.find(ns, w, exact=True)
                if n:
                    phone.tap(serial, *n["center"])
                    break
            time.sleep(1.2)
            quiet = 0
            continue
        quiet += 1
        if quiet >= 3:
            break
        time.sleep(0.8)
    return seen


def main() -> None:
    serial = phone.pick()
    phone.heartbeat("adb")  # 界面「手机」卡显示 USB 在线
    count = 500
    for i, a in enumerate(sys.argv):
        if a == "--count" and i + 1 < len(sys.argv):
            count = int(sys.argv[i + 1])

    items = pp.load_pending()[:count]
    text = ",".join(items)
    print(f"[设备] {serial}")
    print(f"[注入] {len(items)} 条 / {len(text)} 字符（ADBKeyboard 一次性提交）")

    t0 = time.time()
    inject(serial, text)
    got = edit_text(serial)
    n_got = len([x for x in got.split(",") if x.strip()])
    print(f"[结果] 耗时 {time.time() - t0:.1f}s，输入框实收 {n_got} 条 / {len(got)} 字符")

    if n_got != len(items):
        print("！！条数不符")
        (ROOT / "tmp" / "inject_got.txt").write_text(got, encoding="utf-8")
        print("   已存 tmp/inject_got.txt")
        phone.shot(serial, ROOT / "tmp" / "inject_fail.png")
        return

    phone.shot(serial, ROOT / "tmp" / "inject_ok.png")

    if "--confirm" not in sys.argv:
        print("[完成] 未点确定")
        return

    phone.tap_text(serial, "确定", exact=True)
    time.sleep(3)
    anomalies = harvest_popups(serial)
    ns = phone.dump_nodes(serial)
    seqs = [
        int(n["text"])
        for n in ns
        if n["text"].strip().isdigit() and n["bounds"][0] < 180 and n["bounds"][1] > 900
    ]
    print(f"[列表] 序号最大 {max(seqs) if seqs else 0}")
    if anomalies:
        print(f"[弹窗] 共 {len(anomalies)} 次：")
        for a in anomalies:
            print("   -", a)
        (ROOT / "tmp" / "anomalies.json").write_text(
            json.dumps(anomalies, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        print("[弹窗] 无")
    phone.shot(serial, ROOT / "tmp" / "after_confirm.png")


if __name__ == "__main__":
    main()
