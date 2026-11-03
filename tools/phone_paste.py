"""把 PC 端待揽收单号批量灌进申行者「揽件扫描」页。

前提：手机已 USB 连接、申行者已登录。脚本会自己导航到批量输入页。

用法：
    python tools/phone_paste.py --slice 0:500        # 取 pending 前 500 条
    python tools/phone_paste.py --slice 3:500 --fill-only
    python tools/phone_paste.py --upload             # 只点上传

单号来源优先级：tmp/pending.json → data/scan.db 的 pending 队列。
"""
from __future__ import annotations

import json
import shlex
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import phone  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKG = "cn.sto.sxz.core.app"
EDIT_HINT = "批量粘贴"
GO_SCAN = "保存去扫描"
CONFIRM = "确定"
UPLOAD = "上传"


def load_pending() -> list[str]:
    cache = ROOT / "tmp" / "pending.json"
    if cache.exists():
        items = json.loads(cache.read_text(encoding="utf-8"))
        if items:
            return [str(x).strip() for x in items if str(x).strip()]
    db = ROOT / "data" / "scan.db"
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT waybill FROM scan_jobs WHERE status = 'pending' ORDER BY id"
        ).fetchall()
    return [str(r[0]).strip() for r in rows if str(r[0]).strip()]


def nodes(serial: str) -> list[dict]:
    return phone.dump_nodes(serial)


def has(nodes: list[dict], text: str) -> bool:
    return phone.find(nodes, text) is not None


def edit_node(nodes: list[dict]) -> dict | None:
    for n in nodes:
        if "EditText" in n["cls"]:
            return n
    return None


def dismiss_blockers(serial: str, ns: list[dict]) -> list[dict]:
    """关掉挡在批量输入页上的弹窗，返回重新 dump 的节点。

    实测踩坑（2026-10-26）：上一轮注入遗留的「温馨提示 —— 【xxx】为拦截件…
    [拦截] [移除并继续]」弹窗会盖住整页，EDIT_HINT 匹配不到，
    ensure_batch_page 就误判「没能进到批量输入页」而反复失败。
    必须点「移除并继续」（把拦截件从本批移除）：
    「拦截」会真的执行拦截操作，绝不能点。

    2026-10-29：补「提示 —— 有N个重复单号,已经过滤 [我知道了]」弹窗
    （上一轮上传成功但回写失败后重发整批必现），点「我知道了」即可，
    跨轮遗留同样会把导航挡死。
    """
    if has(ns, "温馨提示") and has(ns, "移除并继续"):
        print("[导航] 关闭拦截件提示弹窗（点「移除并继续」）")
        phone.tap_text(serial, "移除并继续", exact=True)
        time.sleep(1)
        ns = nodes(serial)
    if has(ns, "我知道了") and has(ns, "提示"):
        print("[导航] 关闭提示弹窗（点「我知道了」）")
        phone.tap_text(serial, "我知道了", exact=True)
        time.sleep(1)
        ns = nodes(serial)
    return ns


def ensure_batch_page(serial: str) -> None:
    """导航到「手动输入/批量粘贴」页，已在则直接返回。"""
    # 手机可能停在桌面/微信：先点亮屏幕并把申行者调到前台（失败就按老路走，别直接放弃）
    state = phone.ensure_app(serial)
    if state == "failed":
        print("[调起] 申行者没拉起来，当前界面：" + " | ".join(phone.front_texts(serial)))
    elif state == "launched":
        print("[调起] 已把申行者调到前台")
    ns = nodes(serial)
    ns = dismiss_blockers(serial, ns)
    if has(ns, EDIT_HINT):
        print("[导航] 已在批量输入页")
        return

    if not has(ns, "揽件扫描"):
        print("[导航] 回首页")
        phone.shell(f"monkey -p {PKG} -c android.intent.category.LAUNCHER 1", serial=serial)
        time.sleep(6)
        ns = nodes(serial)
        ns = dismiss_blockers(serial, ns)

    # 只有停在首页时才点「揽件扫描」入口；已经在扫描页就别去点标题（点不出东西）
    on_home = any(has(ns, t) for t in ("派件任务", "签收扫描", "揽件任务"))
    if on_home and has(ns, "揽件扫描") and not has(ns, GO_SCAN):
        print("[导航] 点「揽件扫描」")
        phone.tap_text(serial, "揽件扫描", exact=True)
        time.sleep(2.5)
        ns = nodes(serial)

    if has(ns, GO_SCAN):
        print("[导航] 点「保存去扫描」")
        phone.tap_text(serial, GO_SCAN, exact=True)
        time.sleep(2.5)
        ns = nodes(serial)
        ns = dismiss_blockers(serial, ns)

    if not has(ns, EDIT_HINT):
        # 扫描页中间那排圆图标，第 4 个是「手动输入」
        print("[导航] 点笔图标（手动输入）")
        phone.tap(serial, 756, 870)
        time.sleep(1.5)
        ns = nodes(serial)
        ns = dismiss_blockers(serial, ns)

    if not has(ns, EDIT_HINT):
        raise SystemExit("没能进到批量输入页，请把手机停在揽件扫描页后重试")


def fill(serial: str, items: list[str], per: int = 25) -> str:
    node = edit_node(nodes(serial))
    if not node:
        raise SystemExit("找不到输入框")
    phone.tap(serial, *node["center"])
    time.sleep(0.8)
    total = len(items)
    for i in range(0, total, per):
        piece = ",".join(items[i : i + per])
        phone.shell(f"input text {shlex.quote(piece)}", serial=serial, timeout=180)
        time.sleep(0.25)
        done = min(i + per, total)
        if done % 100 < per:
            print(f"  灌入 {done}/{total}")
    time.sleep(1.5)
    return edit_node(nodes(serial))["text"] if edit_node(nodes(serial)) else ""


def confirm(serial: str) -> tuple[int, list[str], list[str]]:
    """点「确定」，返回 (列表最大序号, 被移除拦截件的弹窗文本列表, 信息弹窗文本列表)。

    拦截件会在点确定后弹「温馨提示 —— 【单号】为拦截件…」，挡住列表生成
    （实测 2026-10-26：列表序号 = 0，弹窗还留在页面上把后面每一轮都挡死）。
    处理：点「移除并继续」把拦截件从本批摘出去，其余单号正常进列表；
    多个拦截件可能连弹，循环处理。被移除的单号由调用方从弹窗文本提取后记 fail。

    「提示 —— 有N个重复单号,已经过滤 [我知道了]」（实测 2026-10-29）：
    重发上一轮已成功上传但未回写的批次时必现，整批被过滤 → 列表序号 = 0。
    处理：点「我知道了」，弹窗文本放 info_dialogs 交调用方按「已揽收」收口。

    节奏（用户要求 2026-10-26）：隔壁手机端钉钉桥在 PC 端没跑时仍会抢前台，
    点完确定几秒内手机就可能被切到钉钉 —— 这里不用固定 4s 等待，
    0.4s 快轮询，一看到列表就返回，看到拦截弹窗立刻收掉。
    """
    phone.tap_text(serial, CONFIRM, exact=True)
    removed_dialogs: list[str] = []
    info_dialogs: list[str] = []
    seqs: list[int] = []
    for _ in range(25):  # 最多 ~10s
        time.sleep(0.4)
        ns = nodes(serial)
        if has(ns, "温馨提示") and has(ns, "移除并继续"):
            for n in ns:
                t = n["text"].strip()
                if "为拦截件" in t or ("拦截" in t and len(t) > 12):
                    if t not in removed_dialogs:
                        removed_dialogs.append(t)
            print("[确定] 拦截件弹窗，点「移除并继续」")
            phone.tap_text(serial, "移除并继续", exact=True)
            time.sleep(0.8)
            continue
        if has(ns, "我知道了") and has(ns, "提示"):
            for n in ns:
                t = n["text"].strip()
                if "重复" in t and t not in info_dialogs:
                    info_dialogs.append(t)
            print("[确定] 提示弹窗（重复单号已过滤），点「我知道了」")
            phone.tap_text(serial, "我知道了", exact=True)
            time.sleep(0.8)
            continue
        seqs = []
        for n in ns:
            t = n["text"].strip()
            if t.isdigit() and n["bounds"][0] < 180 and n["bounds"][1] > 900:
                seqs.append(int(t))
        if seqs:
            break
    return (max(seqs) if seqs else 0), removed_dialogs, info_dialogs


def do_upload(serial: str) -> None:
    ns = nodes(serial)
    n = phone.find(ns, UPLOAD)
    if not n:
        raise SystemExit("找不到「上传」按钮")
    print(f"[上传] 点 {n['text']}")
    phone.tap(serial, *n["center"])
    time.sleep(6)
    ns = nodes(serial)
    texts = [x["text"].strip() for x in ns if x["text"].strip()]
    print("[上传后] " + " | ".join(texts[:14]))
    phone.shot(serial, ROOT / "tmp" / "phone_upload.png")


def main() -> None:
    serial = phone.pick()
    phone.heartbeat("adb")  # 界面「手机」卡显示 USB 在线
    items = load_pending()
    print(f"[设备] {serial}  待扫 {len(items)} 条")

    sl = "0:500"
    for i, a in enumerate(sys.argv):
        if a == "--slice" and i + 1 < len(sys.argv):
            sl = sys.argv[i + 1]
    a, _, b = sl.partition(":")
    part = items[int(a or 0) : int(b or len(items))]
    print(f"[切片] {sl} → {len(part)} 条，首 {part[0] if part else '-'}")

    ensure_batch_page(serial)
    if "--upload" in sys.argv:
        do_upload(serial)
        return

    got = fill(serial, part)
    filled = len([x for x in got.split(",") if x.strip()])
    print(f"[输入框] 实收 {filled} 条 / 预期 {len(part)} 条")
    if filled != len(part):
        print("！！条数不符，先别点确定，检查是否有丢字")
        phone.shot(serial, ROOT / "tmp" / "phone_fill.png")
        path = ROOT / "tmp" / "phone_fill.txt"
        path.write_text(got, encoding="utf-8")
        print(f"   已存 {path}，可人工核对")
        if "--force" not in sys.argv:
            return

    if "--fill-only" in sys.argv:
        phone.shot(serial, ROOT / "tmp" / "phone_fill.png")
        print("[完成] 只灌入未确定")
        return

    n, removed_dlg, info_dlg = confirm(serial)
    print(f"[确定] 列表序号最大 = {n}")
    if info_dlg:
        print(f"[确定] 信息弹窗：{info_dlg}")
    phone.shot(serial, ROOT / "tmp" / "phone_list.png")
    if n != len(part):
        print("！！列表条数与灌入不符，先人工看一眼再上传")


if __name__ == "__main__":
    main()
