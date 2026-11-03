"""HTTP 服务：给界面用，也给手机端的扫描执行器用。

为什么用标准库 http.server 而不是 FastAPI：
- 这是个内网单机工具，端点一共十来个，FastAPI 的收益抵不过打包体积与额外依赖；
- ddwx-bridge 也是同一路子，维护心智一致；
- 真要换框架，业务逻辑都在 JobService 里，迁移成本很低。
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import mimetypes
import re
import socket
import sys
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .config import Config
from .errors import AppError, NotFoundError, UnauthorizedError, ValidationError
from .log import get_logger, request_id_var
from .rules import MAX_LENGTH, RuleStore
from .service import JobService

log = get_logger("scan.server")

MAX_BODY = 1024 * 1024
WEB_DIR = Path(__file__).resolve().parent.parent / "app" / "web"


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self._routes.append((method.upper(), re.compile(f"^{pattern}$"), handler))

    def match(self, method: str, path: str) -> tuple[Callable[..., Any], dict[str, str]] | None:
        for m, regex, handler in self._routes:
            if m != method.upper():
                continue
            found = regex.match(path)
            if found:
                return handler, found.groupdict()
        return None


class App:
    def __init__(self, config: Config, rules: RuleStore | None = None) -> None:
        self.config = config
        # 先建规则：服务要按它取「单批上限 / 重试次数」，界面上改完立即生效
        self.rules = rules if rules is not None else RuleStore(config)
        # 与微信采集线程共用同一个实例：界面上改规则，采集线程下一轮立刻看到
        self.service = JobService(config.db_path, config, self.rules)
        # 界面上「启用微信采集」时用它按需拉起采集线程（由 run/desktop 注入）
        self.wechat_starter: Callable[[], None] | None = None
        # 手机脚本这一轮拿到的是哪批单号（batch_id → [waybill]），回传结果时按它结算
        self._phone_hand: dict[str, list[str]] = {}
        self.router = Router()
        self._register()

    # ---------- 路由表 ----------

    def _register(self) -> None:
        r = self.router
        r.add("GET", r"/health", self.health)
        r.add("GET", r"/ready", self.ready)
        r.add("GET", r"/api/stats", self.stats)
        r.add("GET", r"/api/stats/daily", self.stats_daily)
        r.add("GET", r"/api/jobs", self.list_jobs)
        r.add("GET", r"/api/jobs/next", self.next_job)
        r.add("POST", r"/api/jobs/(?P<job_id>\d+)/result", self.job_result)
        r.add("POST", r"/api/jobs/(?P<job_id>\d+)/retry", self.job_retry)
        r.add("POST", r"/api/jobs/(?P<job_id>\d+)/skip", self.job_skip)
        r.add("POST", r"/api/jobs/clear-pending", self.jobs_clear_pending)
        r.add("POST", r"/api/jobs/clear-retry", self.jobs_clear_retry)
        # 「立即抓取」：给监听进程留话，让它马上跑一轮（不在这里动手机）
        r.add("POST", r"/api/run/now", self.run_now)
        r.add("POST", r"/api/run/peek", self.run_peek)
        r.add("POST", r"/api/run/take", self.run_take)
        r.add("POST", r"/api/batches", self.create_batch)
        r.add("GET", r"/api/batches/(?P<batch_id>[\w\-]+)", self.get_batch)
        r.add("POST", r"/api/batches/(?P<batch_id>[\w\-]+)/status", self.batch_status)
        r.add("POST", r"/api/imports/excel", self.import_excel)
        r.add("POST", r"/api/imports/inbox", self.import_inbox)
        r.add("POST", r"/api/heartbeat", self.heartbeat)
        r.add("GET", r"/api/events", self.events)
        r.add("GET", r"/api/settings", self.get_settings)
        r.add("POST", r"/api/service", self.set_service)
        r.add("GET", r"/api/wechat/status", self.wechat_status)
        r.add("POST", r"/api/wechat/sessions", self.wechat_sessions)
        r.add("POST", r"/api/wechat/open", self.wechat_open)
        r.add("GET", r"/api/settings/scan", self.get_scan_rules)
        r.add("POST", r"/api/settings/scan", self.set_scan_rules)
        r.add("POST", r"/api/settings/wechat", self.set_wechat_settings)
        r.add("POST", r"/api/settings/limits", self.set_limits)
        r.add("POST", r"/api/settings/preview", self.preview_waybills)
        # 手机取单：申行者「揽件扫描」页能一次粘贴 500 个单号，
        # 所以手机端不用装 App —— 扫码拿到文本，粘贴一次即可。
        r.add("POST", r"/api/mobile/ticket", self.mobile_ticket)
        r.add("GET", r"/api/mobile/text", self.mobile_text)
        r.add("GET", r"/api/mobile/qr.png", self.mobile_qr)
        r.add("GET", r"/m/(?P<ticket>[A-Za-z0-9\-_.]+)", self.mobile_page)
        # 手机端自动化（AutoJs6 脚本）：脚本用 ?k=<token> 自证，不需要票据也不需要请求头
        r.add("GET", r"/api/phone/next", self.phone_next)
        r.add("POST", r"/api/phone/heartbeat", self.phone_heartbeat)
        r.add("POST", r"/api/phone/dump", self.phone_dump)
        r.add("POST", r"/api/phone/report", self.phone_report)

    # ---------- handlers ----------

    def health(self, req: "Request") -> tuple[int, Any]:
        return 200, {"ok": True, "service": "sto-scan-assistant"}

    def ready(self, req: "Request") -> tuple[int, Any]:
        with self.service._conn() as conn:  # noqa: SLF001 - 就绪探针只做一次查询
            conn.execute("SELECT 1").fetchone()
        return 200, {"ok": True, "db": str(self.config.db_path)}

    # ---------- 手机取单 ----------
    # 申行者「揽件扫描」页原生支持一次粘贴 500 个单号（逗号/分号/空格自动分行），
    # 所以手机侧根本不需要装 App、也不需要逐条扫码：
    # 电脑这边把本批单号拼成一段文本 → 手机扫二维码打开页面 → 点「复制」→
    # 切到申行者粘贴 → 确定。票据是自签名的（batch_id.exp.sig），不落库，过期即废。

    MOBILE_TTL = 900  # 15 分钟：够走到手机旁边，又不会让二维码长期有效

    def _mobile_secret(self) -> bytes:
        # 拿 server.token 当签名密钥；没配 token 时退回固定串（纯本机小规模才会出现）
        return (self.config.server.token or "miao-mobile").encode("utf-8")

    def _sign(self, batch_id: str, exp: int) -> str:
        msg = f"{batch_id}.{exp}".encode("utf-8")
        return hmac.new(self._mobile_secret(), msg, hashlib.sha256).hexdigest()[:20]

    def _make_ticket(self, batch_id: str) -> str:
        exp = int(time.time()) + self.MOBILE_TTL
        return f"{batch_id}.{exp}.{self._sign(batch_id, exp)}"

    def _verify_ticket(self, ticket: str) -> str:
        parts = str(ticket or "").split(".")
        if len(parts) != 3:
            raise UnauthorizedError("链接不完整，请在电脑上重新点「发到手机」")
        batch_id, exp_text, sig = parts
        try:
            exp = int(exp_text)
        except ValueError:
            raise UnauthorizedError("链接格式不对，请重新生成二维码")
        if exp < time.time():
            raise UnauthorizedError("二维码过期了，在电脑上重新点一次「发到手机」")
        if not hmac.compare_digest(sig, self._sign(batch_id, exp)):
            raise UnauthorizedError("链接已失效，请重新生成二维码")
        return batch_id

    def _lan_url(self) -> str:
        """选出走默认路由的那块网卡地址，拼成手机能访问的地址。

        不用 socket.gethostbyname(gethostname())：多网卡（虚拟网卡、VPN）时常拿到错的。
        """
        ip = "127.0.0.1"
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("223.5.5.5", 80))  # UDP connect 不发包，只让系统挑出口
                ip = probe.getsockname()[0]
            finally:
                probe.close()
        except OSError:
            pass
        return f"http://{ip}:{self.config.server.port}"

    def _mobile_waybills(self, batch_id: str, only_today: bool = True) -> list[str]:
        """发给手机的单号。默认只给「当天入库」的，昨天的绝不外发。"""
        sql = "SELECT waybill FROM scan_jobs WHERE batch_id = ? AND status = 'pending'"
        params: list[Any] = [batch_id]
        if only_today:
            sql += " AND day = ?"
            params.append(self.service.today())
        sql += " ORDER BY id"
        with self.service._conn() as conn:  # noqa: SLF001 - 只读一次，不进业务层
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [str(r["waybill"]) for r in rows]

    def mobile_ticket(self, req: "Request") -> tuple[int, Any]:
        """给界面用：生成本批的取单链接 + 二维码票据。"""
        body = req.json(optional=True)
        batch_id = str(body.get("batch_id") or "").strip()
        if not batch_id:
            self.service.rollover_stale_batch()
            active = self.service.active_batch() or self.service.auto_dispatch(actor="phone")
            batch_id = str(active["id"]) if active else ""
        if not batch_id:
            raise ValidationError("今天还没有待扫单号，等采集入库自动开批次后再发")
        if not self.service.get_batch(batch_id):
            raise NotFoundError(f"批次不存在：{batch_id}")
        rows = self._mobile_waybills(batch_id)
        if not rows:
            raise ValidationError("今天这一批已经扫完了，没有待扫单号")
        ticket = self._make_ticket(batch_id)
        return 200, {
            "ticket": ticket,
            "batch_id": batch_id,
            "count": len(rows),
            "url": f"{self._lan_url()}/m/{ticket}",
            "expires_in": self.MOBILE_TTL,
        }

    def mobile_text(self, req: "Request") -> tuple[int, Any]:
        """手机页取文本。逗号分隔：申行者会自己按分隔符分行。"""
        batch_id = self._verify_ticket(req.query.get("t", ""))
        rows = self._mobile_waybills(batch_id)
        return 200, {
            "batch_id": batch_id,
            "count": len(rows),
            "text": ",".join(rows),
            "sample": rows[:3],
        }

    def mobile_qr(self, req: "Request") -> tuple[int, Any]:
        ticket = req.query.get("t", "")
        self._verify_ticket(ticket)
        try:
            import qrcode  # 延迟导入：没装这个依赖也不该拖垮整个服务
        except ImportError:
            raise NotFoundError("缺少 qrcode 依赖，请 pip install qrcode")
        img = qrcode.make(f"{self._lan_url()}/m/{ticket}")
        buf = io.BytesIO()
        img.save(buf)
        return 200, buf.getvalue(), "image/png"

    def mobile_page(self, req: "Request", ticket: str) -> tuple[int, Any]:
        self._verify_ticket(ticket)  # 票据不对就别给页面，免得手机上白屏
        page = WEB_DIR / "mobile.html"
        if not page.exists():
            raise NotFoundError("手机页缺失")
        return 200, page.read_bytes(), "text/html; charset=utf-8"

    # ---------- 手机端自动化（AutoJs6 脚本） ----------
    # 手机脚本在后台轮询 /api/phone/next 拿本批单号 → 写剪贴板 → 粘进申行者 → 点确定
    # → 把失败单号 / 完成信号回传 /api/phone/report。key 直接用 server.token，
    # 只在局域网内可达，且和界面 API 的 X-Token 是同一条凭据。

    def _require_phone_key(self, req: "Request") -> None:
        key = str(req.query.get("k") or "").strip()
        want = (self.config.server.token or "").strip()
        if not want or not key or not hmac.compare_digest(key, want):
            raise UnauthorizedError("手机脚本的 k 不对，请用界面里给出的地址")

    def phone_heartbeat(self, req: "Request") -> tuple[int, Any]:
        """PC 端 adb 驱动每次操作前打个点，界面上的「手机」卡才不会一直显示失联。"""
        self._require_phone_key(req)
        page = str(req.query.get("page") or "adb")
        self.service.heartbeat(page=page, battery="")
        return 200, {"ok": True, "page": page}

    def phone_next(self, req: "Request") -> tuple[int, Any]:
        """给手机脚本：当前批次「当天」的待扫单号（逗号分隔，申行者自己会分行）。

        昨天的单号绝不外发：跨天时旧批次自动收尾，新批次只装当天入库的单号。
        """
        self._require_phone_key(req)
        today = self.service.today()
        if self.service.rollover_stale_batch(actor="phone"):
            log.info("手机拉单触发跨天收尾")
        active = self.service.active_batch()
        if not active:
            active = self.service.auto_dispatch(actor="phone")
        if not active:
            return 200, {
                "batch_id": None,
                "date": today,
                "count": 0,
                "text": "",
                "ts": int(time.time()),
            }
        batch_id = str(active["id"])
        rows = self._mobile_waybills(batch_id)[: self.config.scan.batch_max]
        # 记下这一轮发出去的是哪几条（按 waybill 记，回传时用它算 ok/fail 全集）
        self._phone_hand[batch_id] = list(rows)
        self.service.heartbeat(page="adb", battery="")
        return 200, {
            "batch_id": batch_id,
            "date": today,
            "count": len(rows),
            "text": ",".join(rows),
            "ts": int(time.time()),
        }

    def phone_dump(self, req: "Request") -> tuple[int, Any]:
        """手机脚本上传当前界面控件树，落在 data/phone_dumps/ 里给开发侧分析。"""
        self._require_phone_key(req)
        body = req.json()
        text = str(body.get("dump") or "")
        tag = re.sub(r"[^\w\-]", "", str(body.get("tag") or "dump"))[:40] or "dump"
        if len(text) > 512 * 1024:
            raise ValidationError("dump 太大（>512KB）")
        out_dir = self.config.db_path.parent / "phone_dumps"
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{tag}.txt"
        p.write_text(text, encoding="utf-8")
        log.info("手机上传界面：%s（%d 字符）", p.name, len(text))
        return 200, {"ok": True, "file": p.name, "chars": len(text)}

    def phone_report(self, req: "Request") -> tuple[int, Any]:
        """手机脚本回传揽收结果：finished=True 时把本批未处理的任务批量落结果。

        两种结算口径：
        1. 显式清单（推荐，adb 脚本用）：body 里带 ``ok`` / ``fail`` 单号列表，
           脚本自己知道这一轮到底提交了哪几条，进程重启丢了 _phone_hand 也不会算错。
        2. 隐式整批（AutoJs6 老脚本）：只带 ``fail``，其余按「这一轮 /next 发出去的」算 ok。
        """
        self._require_phone_key(req)
        body = req.json()
        batch_id = str(body.get("batch_id") or "").strip()
        if not batch_id or not self.service.get_batch(batch_id):
            raise NotFoundError(f"批次不存在：{batch_id}")

        # fail 既接受 ["77..."]，也接受 [{"waybill":"77...","reason":"超重","final":true}]
        # final=true：这单注定揽不了（如拦截件被弹窗移除），直接落终态不回 pending
        fail_reason: dict[str, str] = {}
        fail_final: set[str] = set()
        for x in body.get("fail") or []:
            if isinstance(x, dict):
                waybill = str(x.get("waybill") or "").strip()
                if waybill:
                    fail_reason[waybill] = str(x.get("reason") or "揽收失败")[:60]
                    if x.get("final"):
                        fail_final.add(waybill)
            else:
                waybill = str(x).strip()
                if waybill:
                    fail_reason[waybill] = "揽收失败"
        ok_list = {str(x).strip() for x in (body.get("ok") or []) if str(x).strip()}
        ok_list -= set(fail_reason)  # 同一个号两边都出现时，以 fail 为准

        finished = bool(body.get("finished"))
        note = str(body.get("note") or "")[:200]
        marked = {"ok": 0, "fail": 0}
        unmatched: list[str] = []
        if finished:
            hand = self._phone_hand.pop(batch_id, None)
            # 带 ok 字段 = 新脚本的显式清单模式；只有 fail 字段 = 老 AutoJs 的整批模式
            explicit = "ok" in body
            targets = (ok_list | set(fail_reason)) if explicit else set(hand or [])
            if targets:  # 没发过单号也没给清单就不结算，免得把别处补进来的单子误判成 ok
                with self.service._conn() as conn:  # noqa: SLF001 - 只读一次就交给业务层
                    rows = [
                        r
                        for r in conn.execute(
                            "SELECT id, waybill FROM scan_jobs WHERE batch_id = ? "
                            "AND status = 'pending' ORDER BY id",
                            (batch_id,),
                        ).fetchall()
                        if str(r["waybill"]) in targets
                    ]
                settled = {str(r["waybill"]) for r in rows}
                for row in rows:
                    waybill = str(row["waybill"])
                    status = "fail" if waybill in fail_reason else "ok"
                    if status == "fail":
                        # 批量路径没走 claim_next，这里补记一次尝试 ——
                        # 不补的话 attempts 恒 0，fail 永远回 pending、批次收不了口
                        self.service.bump_attempt(int(row["id"]))
                    self.service.report(
                        int(row["id"]),
                        status,
                        raw=note or "申行者批量揽收",
                        error=fail_reason.get(waybill, "") if status == "fail" else "",
                        actor="phone",
                        final=waybill in fail_final,
                    )
                    marked[status] += 1
                # 清单里有、但本批查不到 pending 的（已结算过 / 不属本批 / 跨天被收走）
                unmatched = sorted(targets - settled)
        return 200, {
            "ok": True,
            "marked": marked,
            "unmatched": unmatched[:50],
            "finished": finished,
        }

    def stats(self, req: "Request") -> tuple[int, Any]:
        data = self.service.stats()
        # 微信采集的实时状态：界面上要能一眼看出「在采哪个群 / 为什么没采」。
        # 之前这块完全不可见，只能翻日志，用户只会看到「没采到消息」。
        data["wechat"] = self._wechat_state()
        return 200, data

    def stats_daily(self, req: "Request") -> tuple[int, Any]:
        days = int(req.query.get("days") or 14)
        return 200, {"days": days, "items": self.service.daily_stats(days)}

    def _wechat_state(self) -> dict[str, Any]:
        """采集器实时状态 + 下一次采集还要等多久（界面倒计时用）。"""
        col = self._collector()
        if col is None:
            return {
                "collector": False,
                "state": "采集线程未启动",
                "interval_sec": self.rules.interval_sec,
                "next_in": 0,
            }
        next_at = float(getattr(col, "next_at", 0) or 0)
        return {
            "collector": True,
            "state": getattr(col, "state", ""),
            # 配置值优先：待机时采集线程会把 interval 归零，界面要的仍是「设置里选的那个」
            "interval_sec": int(self.rules.interval_sec or 0),
            # 0 表示「马上/正在采」；>0 是距离下一次读微信的秒数
            "next_in": max(0, round(next_at - time.time())) if next_at else 0,
        }

    def list_jobs(self, req: "Request") -> tuple[int, Any]:
        jobs = self.service.list_jobs(
            status=req.query.get("status"),
            batch_id=req.query.get("batch_id"),
            day=req.query.get("day"),
            limit=int(req.query.get("limit") or 50),
        )
        return 200, {"jobs": jobs}

    def next_job(self, req: "Request") -> tuple[int, Any]:
        job = self.service.claim_next(req.query.get("batch_id"))
        return 200, {"job": job}

    def job_result(self, req: "Request", job_id: str) -> tuple[int, Any]:
        body = req.json()
        if "status" not in body:
            raise ValidationError("缺少 status 字段（ok / dup / fail）")
        job = self.service.report(
            int(job_id),
            str(body["status"]),
            raw=str(body.get("raw") or ""),
            error=str(body.get("error") or ""),
            screenshot=str(body.get("screenshot") or ""),
        )
        return 200, {"job": job}

    def job_retry(self, req: "Request", job_id: str) -> tuple[int, Any]:
        return 200, {"job": self.service.retry(int(job_id))}

    def job_skip(self, req: "Request", job_id: str) -> tuple[int, Any]:
        body = req.json(optional=True)
        return 200, {"job": self.service.skip(int(job_id), str(body.get("reason") or ""))}

    def jobs_clear_pending(self, req: "Request") -> tuple[int, Any]:
        body = req.json(optional=True) or {}
        day = str(body.get("day") or "").strip() or None
        return 200, self.service.clear_pending(day=day, actor="operator")

    def jobs_clear_retry(self, req: "Request") -> tuple[int, Any]:
        """一键清空可重试列表（fail + skipped），全部置为 dropped 终态。"""
        body = req.json(optional=True) or {}
        day = str(body.get("day") or "").strip() or None
        return 200, self.service.clear_retry(day=day, actor="operator")

    def run_now(self, req: "Request") -> tuple[int, Any]:
        """点「立即抓取」：**先回微信群采一遍新单**，再让手机去揽。

        为什么先采集：这个名字的语义是「现在就把单号抓进来」。
        早上点了按钮却因为「队列里还没单」什么都不发生，用户会以为坏了 ——
        正确的顺序是「定位微信群 → 采集新单号入队 → 下发手机揽收」。

        采集失败不阻断：微信没开、群名不对时仍然要把已有的积压交给手机，
        那是两件独立的事。失败原因放进 collect 字段，界面照实显示。
        """
        body = req.json(optional=True) or {}
        want_collect = body.get("collect", True)

        collect: dict[str, Any] = {"ok": False, "skipped": True}
        if want_collect:
            col = self._collector()
            if col is None:
                collect = {"ok": False, "error": "采集线程没在运行（服务总开关关着？）"}
            else:
                try:
                    # 60 秒：要打开微信、必要时切群（可能要滚会话列表）、读屏、
                    # 解析表格文件。用户点按钮后是盯着界面等的，宁可多等一会。
                    stats = col.submit("collect", timeout=60)
                    collect = {
                        "ok": True,
                        # new=文本新增、inserted=表格里新解析出的单号
                        "new": int(stats.get("new", 0) or 0),
                        "inserted": int(stats.get("inserted", 0) or 0),
                        "files": int(stats.get("files", 0) or 0),
                        "baselined": int(stats.get("baselined", 0) or 0),
                    }
                    log.info("「立即抓取」已先完成一轮微信群采集", extra={"status": str(collect)})
                except Exception as exc:  # noqa: BLE001 - 采集失败不该让整个按钮报错
                    collect = {"ok": False, "error": str(exc)}
                    log.warning("「立即抓取」采集失败：%s（继续下发已有的积压）", exc)

        # 不管采集成没成，都通知监听去操作手机 —— 队列里可能本来就有积压。
        snap = self.service.request_run_now(actor="operator")
        return 200, {**snap, "collect": collect}

    def run_peek(self, req: "Request") -> tuple[int, Any]:
        """只读探测有没有「立即抓取」请求，**不消费**。

        给监听进程的等待循环用：每 2 秒瞄一眼，探到就跳出去正经取一次。
        如果探测也消费，请求会被等待循环吞掉，永远轮不到执行。
        """
        self._require_phone_key(req)
        pending = self.service.run_request_pending()
        return 200, {"requested_at": "1" if pending else "", "pending": pending}

    def run_take(self, req: "Request") -> tuple[int, Any]:
        """监听进程取单。取值即消费，同一个请求只会跑一轮。"""
        self._require_phone_key(req)
        stamp = self.service.take_run_request(actor="phone")
        return 200, {"requested_at": stamp, "accepted": bool(stamp)}

    def create_batch(self, req: "Request") -> tuple[int, Any]:
        body = req.json()
        limit = int(body.get("limit") or self.config.scan.batch_max)
        batch = self.service.start_batch(limit, note=str(body.get("note") or ""))
        return 201, {"batch": batch}

    def get_batch(self, req: "Request", batch_id: str) -> tuple[int, Any]:
        batch = self.service.get_batch(batch_id)
        if not batch:
            raise NotFoundError(f"批次不存在：{batch_id}")
        return 200, {"batch": batch}

    def batch_status(self, req: "Request", batch_id: str) -> tuple[int, Any]:
        action = str(req.json().get("action") or "")
        return 200, {"batch": self.service.set_batch_status(batch_id, action)}

    def import_excel(self, req: "Request") -> tuple[int, Any]:
        body = req.json()
        raw_path = str(body.get("path") or "").strip()
        if not raw_path:
            raise ValidationError("缺少 path 字段")
        return 200, self._import_path(Path(raw_path))

    def import_inbox(self, req: "Request") -> tuple[int, Any]:
        inbox = self.config.excel.inbox_dir
        results = []
        for path in sorted(inbox.glob("*")):
            if path.suffix.lower() in (".xlsx", ".xlsm", ".csv") and not path.name.startswith("~$"):
                try:
                    results.append(self._import_path(path))
                except Exception as exc:  # 单个文件坏了不能拖垮整批
                    results.append({"file": path.name, "ok": False, "error": str(exc)})
        return 200, {"results": results}

    def _import_path(self, path: Path) -> dict[str, Any]:
        from .extract import extract_file

        if not path.is_absolute():
            path = self.config.root / path
        if not path.exists():
            raise NotFoundError(f"文件不存在：{path.name}")
        # 只允许读 inbox 目录和明确传入的绝对路径，避免变成任意文件读取器
        result = extract_file(path, self.config.scan.prefixes, self.config.scan.lengths)
        ref = f"{path.name}:{result.digest[:16]}"
        if self.service.import_seen("excel", ref):
            return {
                "file": path.name, "ok": True, "skipped": True,
                "reason": "同一份文件（内容哈希一致）已经导入过",
                "total": 0, "inserted": 0, "duplicated": 0,
            }
        enq = self.service.enqueue(
            result.waybills, source="excel", source_ref=result.path.name, actor="operator"
        )
        self.service.record_import(
            "excel", ref, path.name, enq.total, enq.inserted, enq.duplicated
        )
        return {
            "file": path.name, "ok": True, "skipped": False,
            "total": enq.total, "inserted": enq.inserted, "duplicated": enq.duplicated,
            "cells_scanned": result.cells_scanned,
            "warnings": result.warnings[:20],
        }

    def heartbeat(self, req: "Request") -> tuple[int, Any]:
        body = req.json(optional=True)
        self.service.heartbeat(page=str(body.get("page") or ""), battery=str(body.get("battery") or ""))
        return 200, {"ok": True}

    def events(self, req: "Request") -> tuple[int, Any]:
        limit = int(req.query.get("limit") or 50)
        with self.service._conn() as conn:  # noqa: SLF001
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
            return 200, {"events": [dict(r) for r in rows]}

    # ---------- 扫描规则（界面上可改） ----------

    def set_service(self, req: "Request") -> tuple[int, Any]:
        """服务总开关：一键开启 / 停止。

        开启后：采集线程开始盯群 → 新单自动入队 → 有积压自动开批下发。
        这是界面上唯一需要人工点的按钮。
        """
        body = req.json()
        if "running" not in body:
            raise ValidationError("缺少 running 字段（true 开启 / false 停止）")
        want = body["running"]
        if isinstance(want, int) and want in (0, 1):
            want = bool(want)
        snap = self.rules.set_running(want, actor=str(body.get("actor") or "operator"))
        if snap["running"]:
            if not snap["group_name"]:
                log.warning("服务已开启，但还没设置采集群")
            elif self.wechat_starter is not None:
                try:
                    self.wechat_starter()
                except Exception:  # noqa: BLE001 - 采集线程起不来不该让接口 500
                    log.exception("启动微信采集线程失败")
        return 200, {"settings": snap, "service": {"running": snap["running"]}}

    def _collector(self):
        """拿采集器实例。

        采集器归采集线程所有，这里只借来发请求 —— UIA 只能在那个线程里跑，
        所以真正的动作是排进队列、由采集线程执行。
        用 sys.modules 取而不是 import：桌面版是从 run 模块进来跑的，
        再 import 一次会和正在运行的进程形成两套全局变量。
        """
        mod = sys.modules.get("run")
        getter = getattr(mod, "wechat_collector", None) if mod is not None else None
        col = getter() if callable(getter) else None
        if col is not None:
            return col
        # `python run.py serve` 时本模块就是 __main__，不叫 run
        main = sys.modules.get("__main__")
        getter = getattr(main, "wechat_collector", None) if main is not None else None
        return getter() if callable(getter) else None

    def wechat_status(self, req: "Request") -> tuple[int, Any]:
        """微信采集实时状态：给界面显示「在采什么 / 为什么没采」。"""
        snap = self.rules.snapshot()
        state = self._wechat_state()
        state.update({
            "target": snap.get("group_name", ""),
            # enabled = 用户设的采集开关（意图）；active = 此刻是否真在采
            # （= 采集开关 AND 总开关）。两者分开，避免「服务停了」被误读成
            # 「用户把采集关了」。
            "enabled": snap.get("enabled"),
            "active": self.rules.enabled,
            "running": snap.get("running"),
        })
        return 200, state

    def wechat_sessions(self, req: "Request") -> tuple[int, Any]:
        """列出微信会话列表（供设置中心选群，避免手打群名打错）。"""
        col = self._collector()
        if col is None:
            raise ValidationError("采集线程还没启动：先点「开启服务」再读取")
        try:
            return 200, {"sessions": col.submit("sessions", timeout=40)}
        except Exception as exc:  # noqa: BLE001 - 微信侧异常原样交给界面
            raise ValidationError(f"读取微信会话失败：{exc}") from exc

    def wechat_open(self, req: "Request") -> tuple[int, Any]:
        """让程序自己在微信里点开某个群（选群后立刻生效，不用手点微信）。"""
        body = req.json()
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValidationError("缺少 name（要打开的群名）")
        col = self._collector()
        if col is None:
            raise ValidationError("采集线程还没启动：先点「开启服务」再切群")
        try:
            opened = col.submit("open", name, timeout=60)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"打开群聊失败：{exc}") from exc
        return 200, {"opened": opened or "", "ok": bool(opened)}

    def get_settings(self, req: "Request") -> tuple[int, Any]:
        """设置中心一次性拿全：规则 + 采集 + 入库边界 + 只读信息。"""
        return 200, {
            "settings": self.rules.snapshot(),
            "max_length": MAX_LENGTH,
            "paths": {
                "db": str(self.config.db_path),
                "inbox": str(self.config.excel.inbox_dir),
            },
        }

    def get_scan_rules(self, req: "Request") -> tuple[int, Any]:
        return 200, {
            "rules": self.rules.snapshot(),
            "max_length": MAX_LENGTH,
        }

    def set_scan_rules(self, req: "Request") -> tuple[int, Any]:
        body = req.json()
        if "prefixes" not in body and "lengths" not in body:
            raise ValidationError("请提供 prefixes（前缀）或 lengths（位数）")
        current = self.rules.snapshot()
        snapshot = self.rules.update(
            body.get("prefixes", current["prefixes"]),
            body.get("lengths", current["lengths"]),
        )
        with self.service._conn() as conn:  # noqa: SLF001 - 规则变更要留审计
            self.service._audit(
                conn,
                "operator",
                "rules.update",
                f"prefixes={snapshot['prefixes']} lengths={snapshot['lengths']}",
            )
        return 200, {"ok": True, "rules": snapshot}

    def set_limits(self, req: "Request") -> tuple[int, Any]:
        body = req.json()
        current = self.rules.snapshot()
        auto_mode = body.get("auto_mode")
        if auto_mode is not None and not isinstance(auto_mode, bool):
            raise ValidationError("auto_mode 只能是 true 或 false")
        snapshot = self.rules.update_limits(
            body.get("batch_max", current["batch_max"]),
            body.get("max_attempts", current["max_attempts"]),
            auto_mode,
        )
        with self.service._conn() as conn:  # noqa: SLF001 - 变更要留审计
            self.service._audit(
                conn,
                "operator",
                "limits.update",
                f"batch_max={snapshot['batch_max']} attempts={snapshot['max_attempts']}",
            )
        return 200, {"ok": True, "rules": snapshot}

    def preview_waybills(self, req: "Request") -> tuple[int, Any]:
        """试算：把一段文字按当前规则过一遍，告诉用户「哪些会入库、为什么没入」。"""
        from .rules import normalize_lengths, normalize_prefixes
        from .waybill import extract_with_rejects

        body = req.json()
        text = str(body.get("text") or "")
        if len(text) > 20000:
            raise ValidationError("试算文本太长了（上限 2 万字）")
        # 用界面上「正在编辑」的规则试算，而不是已保存的——所见即所得
        prefixes = (
            normalize_prefixes(body["prefixes"])
            if body.get("prefixes") is not None
            else self.rules.prefixes
        )
        lengths = (
            normalize_lengths(body["lengths"])
            if body.get("lengths") is not None
            else self.rules.lengths
        )
        hits, rejects = extract_with_rejects(text, prefixes, lengths)
        return 200, {
            "hits": hits[:200],
            "rejects": [{"value": r.raw, "reason": r.reason} for r in rejects[:200]],
        }

    def set_wechat_settings(self, req: "Request") -> tuple[int, Any]:
        body = req.json()
        if "group_name" not in body:
            raise ValidationError("请提供 group_name（要采集的微信群名）")
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValidationError("enabled 只能是 true 或 false")
        snapshot = self.rules.update_group(
            body["group_name"],
            enabled,
            window=body.get("window"),
            window_start=body.get("window_start"),
            window_end=body.get("window_end"),
            interval_sec=body.get("interval_sec"),
        )
        if enabled and self.wechat_starter is not None:
            self.wechat_starter()  # 幂等：内部只会真正启动一次
        with self.service._conn() as conn:  # noqa: SLF001 - 变更要留审计
            self.service._audit(
                conn,
                "operator",
                "wechat.group",
                f"group={snapshot['group_name'] or '(空)'} "
                f"interval={snapshot['interval_sec']}s",
            )
        return 200, {"ok": True, "rules": snapshot}


class Request:
    def __init__(self, handler: "Handler") -> None:
        parsed = urlparse(handler.path)
        self.path = parsed.path
        self.query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.headers = handler.headers
        self.handler = handler

    def json(self, optional: bool = False) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            if optional:
                return {}
            raise ValidationError("请求体不能为空")
        if length > MAX_BODY:
            raise ValidationError("请求体过大")
        raw = self.handler.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data


class Handler(BaseHTTPRequestHandler):
    server_version = "sto-scan/0.1"
    app: App

    # ---------- 基础设施 ----------

    def log_message(self, fmt: str, *args: Any) -> None:  # 关掉默认的 stderr 日志
        return

    def _client_is_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1", "localhost")

    def _authorized(self) -> bool:
        if self._client_is_local():
            return True
        token = self.headers.get("X-Token") or ""
        return bool(self.app.config.server.token) and hmac.compare_digest(
            token, self.app.config.server.token
        )

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8",
              extra_headers: dict | None = None) -> None:
        if payload is None:  # handler 自己已经把字节写出去了（图片/HTML）
            return
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, bytes):
            body = payload
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            raise NotFoundError("页面不存在")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        # 界面文件必须禁缓存：CEF 有磁盘缓存（cef_cache/），升级后界面还是旧版
        # 「看不到新按钮」就是这么来的 —— 服务端换了文件，浏览器还在用缓存的。
        # HTML/JS 每次都重新拉：本地回环，开销可以忽略。
        self._send(200, path.read_bytes(), ctype,
                   extra_headers={"Cache-Control": "no-store"})

    def _handle(self, method: str) -> None:
        token = uuid.uuid4().hex[:12]
        request_id_var.set(token)
        req = Request(self)
        try:
            # /m/ 与 /api/mobile/ 用 URL 里的票据自证：手机浏览器带不了自定义请求头，
            # 票据本身含过期时间与签名，验不过直接 401。
            if not self._authorized() and not req.path.startswith(("/m/", "/api/mobile/", "/api/phone/")):
                raise UnauthorizedError("缺少或错误的 X-Token")

            matched = self.app.router.match(method, req.path)
            if not matched:
                if method == "GET" and not req.path.startswith("/api/"):
                    return self._serve_static(req.path)
                raise NotFoundError(f"没有这个接口：{method} {req.path}")
            handler, kwargs = matched
            result = handler(req, **kwargs)
            if isinstance(result, tuple) and len(result) == 3:
                # 少数 handler 要自定义 Content-Type（二维码 PNG、手机页 HTML）
                status, payload, ctype = result
                self._send(status, payload, ctype)
                return
            status, payload = result
            if isinstance(payload, dict) and "request_id" not in payload:
                payload["request_id"] = token
            self._send(status, payload)
        except AppError as exc:
            log.warning("请求失败 %s %s: %s", method, req.path, exc.message,
                        extra={"path": req.path})
            self._send(exc.http_status, {
                "error": {"code": exc.code, "message": exc.message},
                "request_id": token,
            })
        except Exception as exc:  # 兜底：绝不把堆栈返回给客户端
            log.exception("未处理异常 %s %s", method, req.path)
            self._send(500, {
                "error": {"code": "internal_error", "message": "服务内部错误，请查看服务端日志"},
                "request_id": token,
            })
        finally:
            request_id_var.set("-")

    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            return self._send_file(WEB_DIR / "index.html")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents:
                raise NotFoundError("页面不存在")
            return self._send_file(target)
        if path == "/favicon.ico":
            fav = WEB_DIR / "static" / "favicon.ico"
            if fav.exists():
                return self._send_file(fav)
            return self._send(204, b"", "image/x-icon")
        raise NotFoundError("页面不存在")

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET")


def make_server(
    config: Config, rules: RuleStore | None = None, wechat_starter: Callable[[], None] | None = None
) -> ThreadingHTTPServer:
    app = App(config, rules)
    app.wechat_starter = wechat_starter
    handler_cls = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((config.server.host, config.server.port), handler_cls)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


def serve(
    config: Config, rules: RuleStore | None = None, wechat_starter: Callable[[], None] | None = None
) -> None:
    try:
        httpd = make_server(config, rules, wechat_starter)
    except OSError as exc:
        log.error("服务端口被占用，无法启动：请检查是否已有另一份程序在运行。%s", exc)
        raise SystemExit(1) from exc
    log.info("服务启动", extra={"path": f"http://{config.server.host}:{config.server.port}"})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，准备退出")
    finally:
        httpd.server_close()
