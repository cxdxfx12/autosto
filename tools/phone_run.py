"""一条命令跑完一轮揽收：取单 → 注入 → 确定 → 上传 → 抓弹窗 → 回写结果。

和 phone_paste / phone_inject 的区别：那两个是「分段手动操作」，
这个是从 PC 队列真的取一批、真的提交、再把结果写回数据库，形成闭环。

用法：
    python tools/phone_run.py --count 5              # 演练：取 5 条 → 灌入 → 点确定 → 停（不上传）
    python tools/phone_run.py --count 5 --live       # 真跑：再点上传，抓弹窗，回写 ok/fail
    python tools/phone_run.py --count 500 --live     # 整批 500
    python tools/phone_run.py --count 5 --live --fail 772083470862628,77...

安全约定：
- 不加 --live 永远停在「确定」之后，不产生任何物流轨迹；
- 回写用显式单号清单（只结算这一轮真正提交的），不依赖服务端内存。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import phone  # noqa: E402
import phone_arbiter as arbiter  # noqa: E402
import phone_paste as pp  # noqa: E402
from phone_inject import broadcast  # noqa: E402

ROOT = phone.ROOT  # 安装包里 tools 在 _internal 下，统一由 phone 那边按 MIAO_ROOT 算好
API = phone.API_BASE


def _harden_stdio() -> None:
    """把 stdout/stderr 换成「永不因编码崩」的包装。

    坑（2026-10-28）：windowed 打包的程序 stdout 跟系统代码页走（GBK），
    日志里出现 ✓ / → 之类的字符就抛 UnicodeEncodeError；
    `PYTHONIOENCODING=utf-8` 对 PyInstaller 程序不生效。
    监听进程因此在一轮跑完的收尾那行崩掉（单子其实已上传成功）。

    做法：优先 reconfigure(errors="replace")；拿不到（windowed 下 stdout
    可能不是 TextIOWrapper）就整体换成包了一层的 TextIOWrapper。
    """
    import io

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            continue
        except (AttributeError, ValueError, OSError):
            pass
        try:
            buffer = getattr(stream, "buffer", None)
            if buffer is None:
                continue
            wrapped = io.TextIOWrapper(
                buffer, encoding=getattr(stream, "encoding", None) or "utf-8",
                errors="replace", line_buffering=True, write_through=True,
            )
            setattr(sys, name, wrapped)
        except (ValueError, OSError, AttributeError):
            continue


_harden_stdio()

OK_WORDS = ("确定", "知道了", "我知道了", "好的", "关闭", "继续", "确认")
BAD_WORDS = ("拦截", "超重", "重复", "已揽收", "已扫描", "失败", "异常", "超过", "限额", "不能")
# 申行者真实界面（2026-10-20 实测）：
#   录入页右上角是「完成」→ 点完才把列表带到扫描页；
#   扫描页底部才是「上传(N)」，resource-id = tvUpload。
# 之前一直找「上传」文案，在录入页根本不存在，脚本会永远卡在第 5 步。
DONE_BTN = "完成"
UPLOAD_ID = "tvUpload"
UPLOAD_PREFIX = "上传"
CONFIRM = "确定"


def find_upload(nodes: list[dict]) -> dict | None:
    """扫描页的「上传(N)」按钮：优先认 resource-id，文字兜底。"""
    for n in nodes:
        if UPLOAD_ID in str(n.get("rid", "")):
            return n
    return phone.find(nodes, UPLOAD_PREFIX)


def goto_scan_page(serial: str) -> None:
    """录入页 → 点「完成」→ 等列表搬到扫描页（已扫 N / 上传(N) 出现）。"""
    nodes = phone.dump_nodes(serial)
    if find_upload(nodes):
        return
    btn = phone.find(nodes, DONE_BTN, exact=True)
    if not btn:
        raise SystemExit("录入页找不到「完成」按钮，当前页面不对")
    print(f"      点「{DONE_BTN}」进入扫描页")
    phone.tap(serial, *btn["center"])
    for _ in range(15):
        time.sleep(0.8)
        if find_upload(phone.dump_nodes(serial)):
            return
    raise SystemExit("点「完成」后没出现「上传」按钮，先人工看一眼手机")


# ---------------- PC 接口 ----------------

def api(path: str, body: dict | None = None, timeout: int = 30) -> dict:
    key = phone._token()
    sep = "&" if "?" in path else "?"
    url = f"{API}{path}{sep}k={key}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        # 绕开系统代理：走代理会把 127.0.0.1 拦成 502（实测踩到）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:240]
        raise SystemExit(f"[接口] {path} → HTTP {exc.code} {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"[接口] 连不上 PC 后端 {API}：{exc.reason}（先启动程序）")


def report_retry(payload: dict, tag: str = "") -> dict | None:
    """回写结果并重试 3 次。

    回写必须重试：上传已提交、回写一旦失败，这批在库里还是 pending，
    下一轮原样重发 → 申行者整批报「重复」（2026-10-29 17:49 白干一轮的根因）。
    3 次均失败返回 None，由调用方落盘 payload 供人工补报。
    """
    t0 = time.time()
    for attempt in range(3):
        try:
            return api("/api/phone/report", payload, timeout=120)
        except Exception as exc:  # SystemExit(HTTPError) / URLError 都算
            print(f"      {tag}回写失败（第 {attempt + 1}/3 次，已过 {time.time() - t0:.0f}s）：{str(exc)[:200]}")
            time.sleep(2)
    return None


def take(count: int, only_take: bool) -> tuple[str, list[str]]:
    if only_take:
        items = pp.load_pending()[:count]
        return "", items
    data = api("/api/phone/next")
    text = str(data.get("text") or "")
    items = [x.strip() for x in text.split(",") if x.strip()]
    if not data.get("batch_id") or not items:
        raise SystemExit("今天没有可下发的单号：先采集/导入，并确认服务总开关是开的")
    return str(data["batch_id"]), items[:count]


# ---------------- 手机操作 ----------------

def inject(serial: str, text: str) -> str:
    # 节奏（用户要求 2026-10-26）：隔壁手机端桥会抢前台，注入完成后每一秒都宝贵，
    # 等待全部压到最短，动作赶在巡检把手机切到钉钉之前。
    node = pp.edit_node(phone.dump_nodes(serial))
    if not node:
        raise SystemExit("找不到批量输入框，当前页面不对")
    phone.tap(serial, *node["center"])
    time.sleep(0.5)
    broadcast(serial, "ADB_CLEAR_TEXT")
    time.sleep(0.3)
    broadcast(serial, "ADB_INPUT_TEXT", text)
    time.sleep(0.8)
    node = pp.edit_node(phone.dump_nodes(serial))
    return node["text"] if node else ""


# 这两类页面都是「可以往下走」的起点：首页 / 扫描页
SAFE_PAGES = ("派件任务", "签收扫描", "请扫描运单条码")
LEAVE_HINT = "数据将丢失"


def reset_to_scan(serial: str) -> None:
    """退回到首页或扫描页，避免上一轮残留的列表/输入态干扰导航。

    坑：批量录入页按返回会弹「返回后手动输入成功的数据将丢失」，
    只按返回是退不出去的，必须点弹窗里的「返回」。
    """
    for _ in range(5):
        nodes = phone.dump_nodes(serial)
        texts = [n["text"].strip() for n in nodes if n["text"].strip()]
        if any(LEAVE_HINT in t for t in texts) and phone.find(nodes, "返回", exact=True):
            phone.tap_text(serial, "返回", exact=True)
            time.sleep(1.6)
            continue
        if any(m in texts for m in SAFE_PAGES) and not phone.find(nodes, "序号"):
            return
        phone.shell("input keyevent 4", serial=serial)
        time.sleep(1.4)


def upload(serial: str, timeout: int) -> dict:
    """点「上传(N)」并盯住界面：弹窗就收掉，计数归零就结束。

    收口判据（实测校准）：按钮不会消失，而是文字从「上传(495)」变成「上传(0)」，
    同时顶部「包裹（已扫 0）」。之前只判断按钮消失，导致大批量上传永远等超时。
    """
    ns0 = phone.dump_nodes(serial)
    n = find_upload(ns0)
    if not n:
        raise SystemExit("扫描页上找不到「上传」按钮，先确认点过确定/完成且列表已生成")
    label = n["text"]
    pending = _pending_count(ns0)
    print(f"[6/7] 点「{label}」（待传 {pending} 条）")
    phone.tap(serial, *n["center"])

    t0 = time.time()
    dialogs: list[str] = []
    snaps: list[list[str]] = []
    quiet = 0
    finished = False
    while time.time() - t0 < timeout:
        time.sleep(1.2)
        nodes = phone.dump_nodes(serial)
        texts = [x["text"].strip() for x in nodes if x["text"].strip()]
        okbtn = next((x for x in nodes if x["text"].strip() in OK_WORDS), None)
        bad = [t for t in texts if any(w in t for w in BAD_WORDS)]
        if okbtn and bad:
            desc = " / ".join(dict.fromkeys(bad))[:160]
            print(f"      弹窗：{desc}")
            dialogs.append(desc)
            phone.tap(serial, *okbtn["center"])
            time.sleep(1.3)
            quiet = 0
            continue
        snaps.append(texts)
        del snaps[:-4]
        left = _pending_count(nodes)
        if left == 0:
            quiet += 1
            if quiet >= 2:
                finished = True
                break
        else:
            quiet = 0
            if int(time.time() - t0) % 15 == 0:
                print(f"      已传 {pending - left}/{pending}")
    return {
        "button": label,
        "pending": pending,
        "finished": finished,
        "elapsed": round(time.time() - t0, 1),
        "dialogs": dialogs,
        "last_texts": snaps[-1] if snaps else [],
    }


def _pending_count(nodes: list[dict]) -> int:
    """从「上传(N)」或「包裹（已扫 N）」读出还剩多少条没传。读不到按 -1。"""
    n = find_upload(nodes)
    if n:
        m = re.search(r"\((\d+)\)", n["text"])
        if m:
            return int(m.group(1))
    for x in nodes:
        m = re.search(r"已扫\s*(\d+)", x["text"])
        if m:
            return int(m.group(1))
    return -1


ADB_IME = "com.android.adbkeyboard/.AdbIME"
HUAWEI_IME = "com.huawei.ohos.inputmethod/com.android.inputmethod.latin.LatinIME"


def current_ime(serial: str) -> str:
    return phone.shell("settings get secure default_input_method", serial=serial).strip()


def use_adb_ime(serial: str) -> None:
    """批量注入依赖 ADBKeyboard 广播，没切过去就静默切（收尾再还）。"""
    if ADB_IME not in current_ime(serial):
        phone.shell(f"ime enable {ADB_IME}", serial=serial)
        phone.shell(f"ime set {ADB_IME}", serial=serial)
        time.sleep(1.0)
        print("      已切换到 ADBKeyboard（广播注入用）")


def restore_ime(serial: str) -> None:
    """收尾把默认输入法还给华为原生的，日常打字才有键盘面板。"""
    if ADB_IME in current_ime(serial):
        phone.shell(f"ime enable {HUAWEI_IME}", serial=serial)
        phone.shell(f"ime set {HUAWEI_IME}", serial=serial)
        print("      输入法已切回华为原生")


# 首页判据：有「派件任务 / 揽件任务」这一排 tab，就不是扫描页
HOME_MARKS = ("派件任务", "揽件任务")

APP_PKG = phone.APP


def _is_home(nodes: list[dict]) -> bool:
    """首页判据：有「派件任务/揽件任务」tab，且没有「上传(N)」按钮。"""
    texts = [n["text"].strip() for n in nodes if n["text"].strip()]
    return any(m in texts for m in HOME_MARKS) and not find_upload(nodes)


def park_app(serial: str, tries: int = 3) -> bool:
    """把申行者最小化到后台（HOME 键），相机随之释放。

    为什么用 HOME 而不是「返回首页」：
      返回要按好几次、每层都可能弹「数据将丢失」，还容易按过头退到桌面，
      而且退到首页会丢掉扫描页刚建好的列表。HOME 一次就把整个 App 送后台，
      camera 跟着 release，下次拉起时页面状态还在，最稳。

    判定：HOME 后前台不再是申行者且屏幕不再是申行者界面 → 成功。
    """
    for _ in range(tries):
        phone.shell("input keyevent 3", serial=serial)  # KEYCODE_HOME
        time.sleep(1.5)
        if phone.focused_pkg(serial) != APP_PKG:
            return True
        print("      HOME 没生效，再按一次")
    return False


def back_to_home(serial: str, tries: int = 4) -> bool:
    """（保留给需要真回首页的场景）逐层返回直到首页。

    坑（实测踩到）：从扫描页按 1 次返回就回首页，但首页要 1-2 秒才渲染。
    按完立刻判断会误判成「还没到」接着按，一路把 App 退到桌面。
    所以按完先等再判断，并且一旦已不在申行者前台就停手。
    日常收尾请用 park_app()，比这个稳。
    """
    for i in range(tries + 1):
        nodes = phone.dump_nodes(serial)
        if _is_home(nodes):
            if i:
                print(f"      已退到申行者首页（返回 {i} 次）")
            return True

        if phone.focused_pkg(serial) != APP_PKG:
            print("      返回按过头了，重新拉起申行者…")
            break

        texts = [n["text"].strip() for n in nodes if n["text"].strip()]
        if any(LEAVE_HINT in t for t in texts) and phone.find(nodes, "返回", exact=True):
            phone.tap_text(serial, "返回", exact=True)
        else:
            phone.shell("input keyevent 4", serial=serial)
        time.sleep(2.2)

    state = phone.ensure_app(serial)
    if state == "failed":
        print("      重新拉起申行者失败（可能锁屏/登录页），需人工看一眼")
        return False
    time.sleep(2.0)
    for _ in range(3):
        nodes = phone.dump_nodes(serial)
        if _is_home(nodes):
            print("      已重新拉起申行者并停在首页")
            return True
        phone.shell("input keyevent 4", serial=serial)
        time.sleep(2.2)
    return False


def finish(serial: str, run: dict, stamp: str) -> None:
    """任何结束路径的统一收尾：放掉相机 + 还输入法 + 落日志。

    「最小化」比「退首页」更好：HOME 一次就把 App 送后台，不用挨层按返回、
    不会踩「数据将丢失」弹窗、也不会按过头退到桌面。

    但最小化会保留页面状态，扫描页里没上传的清单会一直挂着。为免它和下一轮
    的单号混在一起，收尾时先把清单清掉（有残留才清），再最小化。
    """
    nodes = phone.dump_nodes(serial)
    left = _pending_count(nodes)
    if phone.focused_pkg(serial) == APP_PKG and left > 0:
        print(f"      扫描页还有 {left} 条未上传，先清掉再最小化…")
        # 退到首页：扫描页按一次返回即可，清单随之作废
        back_to_home(serial)

    if not park_app(serial):
        print("      ！没能把申行者切到后台，请人工看一眼（相机可能仍在占用）")
        phone.shot(serial, ROOT / "tmp" / f"run-{stamp}-stuck.png")
    restore_ime(serial)
    _save(run, stamp)


def main() -> None:
    argv = sys.argv[1:]
    count = 5
    if "--count" in argv:
        count = int(argv[argv.index("--count") + 1])
    live = "--live" in argv
    only_take = "--no-take" in argv
    timeout = 150
    if "--timeout" in argv:
        timeout = int(argv[argv.index("--timeout") + 1])
    fails: list[str] = []
    if "--fail" in argv:
        fails = [x.strip() for x in argv[argv.index("--fail") + 1].replace("，", ",").split(",") if x.strip()]

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run: dict = {"at": stamp, "count": count, "live": live, "steps": []}

    print(f"[1/7] 取单（count={count}）")
    batch_id, items = take(count, only_take)
    print(f"      批次 {batch_id or '(本地 pending.json)'} / {len(items)} 条，首条 {items[0] if items else '-'}")
    if not items:
        raise SystemExit("没有可发的单号")
    run.update({"batch_id": batch_id, "waybills": items})

    serial = phone.pick()
    phone.heartbeat("adb")
    print(f"[2/7] 设备 {serial}")

    # 和隔壁「钉钉微信转发桥」打个招呼：接下来这几秒前台归我。
    # 它每 5/10/15 分钟巡检一轮，会打开最近任务、把钉钉拉回前台 ——
    # 正好撞上我们点「确定」的话，那一下会落到最近任务界面上，
    # 表现是「注入成功但列表序号 = 0」，整批白跑（2026-10-20 废了 176 条）。
    # 放在碰手机之前：取单只是查 API，不影响它；一旦开始操作就得独占。
    arbiter.ask_peer_yield(arbiter.DEFAULT_SECONDS)

    print("[3/7] 调起申行者 + 导航到批量输入页")
    state = phone.ensure_app(serial)
    if state == "failed":
        print("      当前界面：" + " | ".join(phone.front_texts(serial)))
        raise SystemExit("没能把申行者调到前台（锁屏有密码 / 停在登录页 / 升级弹窗），先人工处理一次")
    print(f"      {state}（focus={phone.focused_pkg(serial)}）")
    reset_to_scan(serial)
    pp.ensure_batch_page(serial)
    use_adb_ime(serial)

    text = ",".join(items)
    print(f"[4/7] 注入 {len(items)} 条 / {len(text)} 字符")
    got = ""
    try:
        got = inject(serial, text)
    finally:
        if not got:
            restore_ime(serial)
    got_n = len([x for x in got.split(",") if x.strip()])
    print(f"      输入框实收 {got_n} 条 / {len(got)} 字符")
    run["injected"] = got_n
    if got_n != len(items):
        phone.shot(serial, ROOT / "tmp" / f"run-{stamp}-inject.png")
        raise SystemExit("条数不符，已中止（未点确定），截图 tmp/run-*-inject.png")

    rows, removed_dialogs, info_dialogs = pp.confirm(serial)
    # 拦截件被「移除并继续」摘出本批：从弹窗文本里提取单号，它们注定揽不了，
    # 记 fail 而不是让整批中止。没处理这个弹窗时表现为「列表序号 = 0」
    # 且弹窗遗留把后面每一轮都挡死（2026-10-26 271 条卡住的根因）。
    removed_wbs: list[str] = []
    for d in removed_dialogs:
        removed_wbs += [w for w in re.findall(r"\d{15}", d) if w in items]
    removed_wbs = list(dict.fromkeys(removed_wbs))
    expect = len(items) - len(removed_wbs)
    print(f"      列表序号最大 = {rows} / 预期 {expect}"
          + (f"（拦截件已移除 {len(removed_wbs)} 条）" if removed_wbs else ""))
    run["rows"] = rows
    if removed_wbs:
        print(f"      移除的拦截件：{removed_wbs}")
        run["removed"] = removed_wbs
    dup_dialogs = [d for d in info_dialogs if "重复" in d]
    if dup_dialogs:
        print(f"      重复提示：{dup_dialogs[-1]}")
        run["dup"] = dup_dialogs
    if rows != expect and dup_dialogs:
        # 口径（用户 2026-10-29）：单号重复 = 这批已揽收。
        # 典型场景：上一轮上传成功但回写失败（接口异常），重发整批时
        # 申行者报「有N个重复单号,已经过滤」→ 列表序号 = 0。
        # 按已揽收整批记 ok，不再上传，直接收口继续下一批。
        print("      [重复] 按已揽收录，跳过上传直接回写")
        if not batch_id:
            print("      单号来自本地缓存，没有批次可回写")
            finish(serial, run, stamp)
            return
        res = report_retry({
            "batch_id": batch_id,
            "finished": True,
            "ok": [x for x in items if x not in removed_wbs],
            "fail": [{"waybill": x, "reason": "拦截", "final": True} for x in removed_wbs],
            "note": "重复单号已过滤，按已揽收录：" + "；".join(dup_dialogs)[:100],
        }, tag="[重复] ")
        if res is None:
            rp = ROOT / "tmp" / f"report-{stamp}-dup.json"
            rp.write_text(json.dumps({
                "batch_id": batch_id,
                "ok": [x for x in items if x not in removed_wbs],
            }, ensure_ascii=False), encoding="utf-8")
            print(f"      ！！回写 3 次均失败，待补报单号已存 {rp}")
            finish(serial, run, stamp)
            raise SystemExit(1)
        marked = res.get("marked", {})
        print(f"      已回写 ok={marked.get('ok', 0)} fail={marked.get('fail', 0)}")
        finish(serial, run, stamp)
        return
    if rows != expect:
        phone.shot(serial, ROOT / "tmp" / f"run-{stamp}-list.png")
        raise SystemExit("列表条数与注入不符，已中止（未上传）")

    goto_scan_page(serial)
    print("      已进入扫描页，列表已带上")
    # 后面只剩点按钮，不需要广播注入了：把输入法还给原生，客户日常打字才有键盘
    restore_ime(serial)

    if not live:
        phone.shot(serial, ROOT / "tmp" / f"run-{stamp}-list.png")
        print("[完成] 停在列表页，未上传。加 --live 才会真正提交。")
        finish(serial, run, stamp)
        return

    print("[5/7] 上传")
    result = upload(serial, timeout)
    run["upload"] = result
    print(f"      耗时 {result['elapsed']}s，弹窗 {len(result['dialogs'])} 次")
    phone.shot(serial, ROOT / "tmp" / f"run-{stamp}-after.png")

    # 上传没有收口（按钮一直在）＝可能压根没提交，绝不能写成 ok
    if not result["finished"]:
        print("！！上传未收口：「上传」按钮一直在。不写回结果，先人工看一眼手机")
        finish(serial, run, stamp)
        return

    # 出现拦截/超重/重复/失败提示，但我们不知道是哪几条 → 不擅自判 ok。
    # 例外：「重复单号,已经过滤」= 已揽收（口径 2026-10-29），不算异常。
    bad = [
        d for d in result["dialogs"]
        if any(w in d for w in ("失败", "拦截", "超重", "异常", "限额"))
        and "重复" not in d
    ]
    if bad and not fails and "--force" not in argv:
        print("！！上传时出现异常弹窗，未自动写回：")
        for d in bad:
            print("      -", d)
        print("   确认失败单号后加 --fail 77...,77... 重跑，或加 --force 全部记 ok")
        finish(serial, run, stamp)
        return

    print("[7/7] 回写结果")
    note = "申行者批量揽收"
    if result["dialogs"]:
        note += "｜弹窗：" + "；".join(result["dialogs"])[:120]
    payload = {
        "batch_id": batch_id,
        "finished": True,
        "ok": [x for x in items if x not in fails and x not in removed_wbs],
        "fail": [{"waybill": x, "reason": "揽收失败"} for x in fails]
        # 弹窗移除的拦截件注定揽不了：final=true 让后端直接落终态，
        # 否则回 pending 后下一轮又被注入又被移除，死循环卡批次收口
        + [{"waybill": x, "reason": "拦截", "final": True} for x in removed_wbs],
        "note": note,
    }
    if not batch_id:
        print("      单号来自本地缓存，没有批次可回写")
        finish(serial, run, stamp)
        return
    res = report_retry(payload)
    if res is not None:
        print(f"      回写返回：marked={res.get('marked')} unmatched={len(res.get('unmatched') or [])}")
    if res is None:
        rp = ROOT / "tmp" / f"report-{stamp}.json"
        rp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print(f"      ！！回写 3 次均失败，payload 已存 {rp}")
        print("      上传已提交成功，切勿重发本批；重启后可用该文件人工补报")
        finish(serial, run, stamp)
        raise SystemExit(1)
    run["report"] = res
    marked = res.get("marked", {})
    print(f"      已回写 ok={marked.get('ok', 0)} fail={marked.get('fail', 0)}")
    if res.get("unmatched"):
        print(f"      未匹配 {len(res['unmatched'])} 条（已结算过或不在本批）：{res['unmatched'][:3]}")
    finish(serial, run, stamp)


def _save(run: dict, stamp: str) -> None:
    p = ROOT / "tmp" / f"run-{stamp}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[日志] {p}")


if __name__ == "__main__":
    # 让路必须在**任何**退出路径上解除：main 里遍地 SystemExit（找不到输入框、
    # 条数不符、列表不对……），靠 finish() 收尾是收不全的。让隔壁白等 3 分钟
    # 不至于出事（它到点自己恢复），但能立刻还回去就立刻还。
    try:
        main()
    finally:
        arbiter.release_peer()
