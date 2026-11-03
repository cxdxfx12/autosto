"""常驻监听：PC 队列有积压 → 自动驱动手机跑一轮揽收。

    python tools/phone_watch.py            # 前台常驻（Ctrl+C 退出）
    python tools/phone_watch.py --once     # 只检查一轮就退出（配合任务计划程序）
    python tools/phone_watch.py --interval 10

这就是「B 方案」的 PC 侧实现：不需要往手机装任何东西，只要 USB 线连着、
本进程开着，采集 → 开批 → 注入 → 上传 → 回写就全自动。

设计要点：
  * 每轮都是独立子进程跑 phone_run.py——任何一次崩溃/中止都带不走下轮的状态；
  * 失败后退避 5 分钟，避免手机弹窗没处理时反复硬闯；
  * 空闲时只打 /api/stats 和心跳，给界面「手机」卡保持在线。
"""

from __future__ import annotations

import argparse
import json
import locale
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import phone  # noqa: E402

# 程序根（config/data/vendor 所在）：安装包里 tools 在 _internal 下，
# 由 scan.phone_supervisor 通过 MIAO_ROOT 指过来。
ROOT = phone.ROOT

POLL_SECONDS = 10
FAIL_COOLDOWN = 300  # 一轮失败后的冷静期（秒）
IDLE_HEARTBEAT = 60  # 空闲时打心跳的间隔（秒）

# 单实例锁的文件句柄（持有到进程退出，不能释放）
_LOCK: object | None = None


_LOG_FH: object | None = None


def _open_log_file() -> None:
    """自己的日志自己落一份 UTF-8。

    为什么不靠 stdout 重定向：安装版是 --windowed 打包的，子进程的 stdout
    编码跟着系统代码页走（中文机器是 GBK），重定向到文件后整篇乱码，
    tail / 编辑器都读不了。这里显式指定编码，谁起的都一致。
    """
    global _LOG_FH
    path = ROOT / "data" / "logs" / "phone_watch.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            head = path.read_bytes()[:4096]
            legacy = True
            try:
                head.decode("utf-8")
                legacy = False
            except UnicodeDecodeError:
                pass
            if legacy or path.stat().st_size > 5 * 1024 * 1024:
                path.replace(path.with_name(path.name + ".old"))
        _LOG_FH = path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        _LOG_FH = None


def log(msg: str) -> None:
    """打一行日志。**绝不允许因为编码问题把监听进程搞崩**。

    坑（2026-10-28 生产事故）：windowed 打包出的程序 stdout 跟系统代码页走
    （GBK），`print("本轮完成 ✓")` 直接抛 UnicodeEncodeError；
    `PYTHONIOENCODING=utf-8` 对 PyInstaller 程序不生效。
    监听进程最外层没有 try 兜住，于是整轮跑完却在收尾那一行崩掉 → 监听退出
    → 只剩 supervisor 重起，日志里反复出现同一段 traceback。
    所以：stdout 一律吞掉异常，真日志靠 UTF-8 文件。
    """
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, ValueError, OSError):
        # ValueError/OSError：windowed 下 stdout 可能为 None 或已关闭
        pass
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — 日志写失败绝不能影响揽收
            pass


def stats() -> dict:
    try:
        return phone_run_api("/api/stats")
    except Exception as e:  # noqa: BLE001 — 常驻脚本绝不能因一次请求就死掉
        log_once(f"查队列失败：{type(e).__name__}: {str(e)[:120]}")
        return {}


_last_err = [""]


def log_once(msg: str) -> None:
    """同样的错误只打一次，避免断网时刷屏。"""
    if msg != _last_err[0]:
        log(msg)
        _last_err[0] = msg


def phone_run_api(path: str) -> dict:
    import urllib.request

    key = phone._token()
    req = urllib.request.Request(f"{phone.API_BASE}{path}?k={key}")
    # 本机回环必须绕过系统代理，否则会被代理拦成 502（实测踩到）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        _last_err[0] = ""
        return json.loads(resp.read().decode("utf-8"))


def take_run_request(probe: bool = False) -> str:
    """取「立即抓取」请求。返回时间戳表示有新请求要跑，空串表示没有。

    `probe=True` 是**只读**探测：只问「有没有」，不消费（服务端 markserved
    只发生在真正的取值上）。用在等待循环里 —— 探到就跳出去，让主循环
    用 probe=False 正经取一次。反过来（探测即消费）会让请求被吞掉、永远不执行。
    """
    import urllib.request

    key = phone._token()
    path = "/api/run/peek" if probe else "/api/run/take"
    req = urllib.request.Request(
        f"{phone.API_BASE}{path}?k={key}", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as resp:
            return str(json.loads(resp.read().decode("utf-8")).get("requested_at") or "")
    except Exception as exc:                 # 取不到就当没请求，绝不因此崩掉监听
        if not probe:
            log_once(f"取「立即抓取」请求失败：{exc}")
        return ""


def sleep_but_listen(seconds: float, step: float = 2.0) -> None:
    """睡眠，但每隔 `step` 秒瞄一眼有没有「立即抓取」请求。

    为什么不用 time.sleep(interval)：默认轮询 10 秒一次，人在界面上点了
    「立即抓取」却还要干等大半个周期，体验上就是「点了没反应」。
    这里把等待切碎，最坏延迟就是 step 秒。

    代价是每 2 秒一次 HTTP —— 但那是本机回环，开销可以忽略；
    而且 `_probe` 只在真有请求时才返回非空，不会误触发执行。
    """
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        time.sleep(min(step, max(0.0, end - time.time())))
        if take_run_request(probe=True):
            return


def _decode(raw: bytes) -> str:
    """子进程输出解码：源码态是 UTF-8，安装版 windowed 的 miao-core 是 GBK。

    先试 UTF-8（GBK 中文字节基本不是合法 UTF-8，能可靠区分），失败再按
    系统代码页解，两边都不至于把日志打成问号。
    """
    for enc in ("utf-8", locale.getpreferredencoding(False) or "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def run_round(count: int = 500) -> tuple[bool, str]:
    """跑一轮 phone_run，返回 (是否成功, 输出尾部)。"""
    if getattr(sys, "frozen", False):
        # 安装版没有独立的 Python 解释器：让本 exe 带参数再进来跑一轮
        # （参数分流在 core_main._run_tool，仍然是独立进程，崩了不影响监听）
        cmd = [sys.executable, "--phone-run", "--count", str(count),
               "--live", "--timeout", "300"]
    else:
        cmd = [sys.executable, str(ROOT / "tools" / "phone_run.py"),
               "--count", str(count), "--live", "--timeout", "300"]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, timeout=900,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    out = _decode(p.stdout or b"").strip().splitlines()
    tail = out[-12:]
    if p.returncode != 0:
        # 失败必须带 stderr：traceback 全在 stderr，之前只看 stdout 排障全靠猜
        err = _decode(p.stderr or b"").strip().splitlines()
        if err:
            tail += ["-- stderr 尾巴 --"] + err[-15:]
    return p.returncode == 0, "\n".join(tail)


def _single_instance() -> object | None:
    """同一时间只允许一个监听在跑。

    现在程序自己会拉起监听（scan.phone_supervisor），现场也可能留着
    `tools/run_all.py` 托管的那个 —— 两份同时活着会抢着往手机注入，
    轻则重复揽收、重则把界面点乱。用文件独占锁挡住后来的那个。

    返回文件对象（必须持有到进程结束，故意不关；被回收时锁自动释放）。
    """
    lock_path = ROOT / "data" / "phone_watch.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+")
        import msvcrt  # 仅 Windows；其它平台没锁也能跑（本来就单实例使用）

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except Exception as exc:  # noqa: BLE001
        log(f"已有一个监听在跑（{type(exc).__name__}），本进程退出")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=POLL_SECONDS)
    ap.add_argument("--once", action="store_true", help="检查一轮就退出")
    args = ap.parse_args()

    global _LOCK
    _LOCK = _single_instance() if not args.once else None
    if not args.once and _LOCK is None:
        return 0

    _open_log_file()
    log(f"常驻监听启动（每 {args.interval}s 查一次队列；Ctrl+C 退出）")
    last_hb = 0.0
    cooldown_until = 0.0

    while True:
        now = time.time()

        # 空闲心跳：让界面上的「手机」卡保持在线
        if now - last_hb >= IDLE_HEARTBEAT:
            phone.heartbeat("watch")
            last_hb = now

        s = stats()
        if not s:
            log("后端没连上（程序没开？）60s 后重试")
            if args.once:
                return 1
            time.sleep(60)
            continue

        pending = int(s.get("pending") or 0)
        running = bool(s.get("service", {}).get("running"))
        batch = s.get("batch") or {}

        # 界面上点「立即抓取」→ 不等下一个轮询周期，马上跑。
        # 这个标记**优先于冷静期**：冷却只是「上一轮失败后别急着重试」的自我克制，
        # 人手动点了就是明确指令，应该照办（失败原因人可能已经处理掉了）。
        wanted = take_run_request()
        manual = bool(wanted)
        if manual:
            log(f"收到「立即抓取」请求（{wanted}），马上执行，不等轮询周期")

        if pending <= 0 or not running:
            if args.once:
                log(f"队列空闲（pending={pending}），退出")
                return 0
            sleep_but_listen(args.interval)
            continue

        if now < cooldown_until and not manual:
            remain = int(cooldown_until - now)
            log(f"冷静期还剩 {remain}s（上一轮失败，等手机状态稳定）")
            if args.once:
                return 1
            # 冷静期里也要留意「立即抓取」：不能傻等 5 分钟不理人。
            sleep_but_listen(min(remain, max(2, args.interval)))
            continue
        if manual and now < cooldown_until:
            log("（仍在冷静期，但人手动点了，跳过等待）")

        log(f"发现积压 {pending} 条（批次 {batch.get('id')}），开始一轮揽收 …")
        ok, tail = run_round()
        for line in tail.splitlines():
            log("  | " + line)
        if ok:
            # 别用 ✓ 这类符号：windowed 的 stdout 是 GBK，会抛 UnicodeEncodeError
            log("本轮完成")
            cooldown_until = 0.0
        else:
            log(f"本轮失败（exit != 0），{FAIL_COOLDOWN}s 内不再尝试")
            cooldown_until = now + FAIL_COOLDOWN

        if args.once:
            return 0 if ok else 1
        sleep_but_listen(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("已停止")
