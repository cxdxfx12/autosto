"""业务核心：入队、批次、领取任务、回报结果、统计。

所有状态流转都只在这里发生，HTTP 层与手机端都不许直接 UPDATE。
这是本项目唯一需要严谨的地方——一条单号被扫两次、或卡在 running 永远不结束，
业务员第二天就会发现，但那时已经很难查了。
"""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import Config
from .db import connect, get_runtime, set_runtime, transaction, utcnow
from .errors import (
    BatchLimitError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .log import get_logger

log = get_logger("scan.service")

VALID_STATUS = ("pending", "running", "ok", "dup", "fail", "skipped", "dropped")
# dropped = 人工「清除重试」作废的单，已是终态，不再出现在可重试列表
FINAL_STATUS = ("ok", "dup", "fail", "skipped", "dropped")


@dataclass(frozen=True)
class EnqueueResult:
    total: int
    inserted: int
    duplicated: int


class JobService:
    def __init__(self, db_path: Path, config: Config, rules=None) -> None:
        self.db_path = db_path
        self.config = config
        # rules 是界面上可改的运行时设置（单批上限 / 重试次数）；没有就退回配置文件
        self.rules = rules

    @property
    def batch_max(self) -> int:
        return self.rules.batch_max if self.rules is not None else self.config.scan.batch_max

    @property
    def session_max(self) -> int:
        """申行者单次扫描上限（默认 500）。超过就自动拆成下一批。"""
        return int(getattr(self.config.scan, "session_max", 500) or 500)

    @property
    def max_attempts(self) -> int:
        return (
            self.rules.max_attempts if self.rules is not None else self.config.scan.max_attempts
        )

    # ---------- 基础 ----------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """每次操作开一个短连接，用完就关（sqlite3 的 with 只提交不关闭）。"""
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
        conn.execute(
            "INSERT INTO events(at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (utcnow(), actor, action, detail),
        )

    # ---------- 入队 ----------

    def enqueue(
        self,
        waybills: Iterable[str],
        *,
        source: str,
        source_ref: str = "",
        actor: str = "system",
    ) -> EnqueueResult:
        items = [w.strip() for w in waybills if w and w.strip()]
        if not items:
            raise ValidationError("没有可入队的单号")

        inserted = 0
        duplicated = 0
        now = utcnow()
        day = now[:10]  # 本地日期（utcnow 带时区偏移，前 10 位就是当地 YYYY-MM-DD）
        with self._conn() as conn:
            with transaction(conn):
                for waybill in dict.fromkeys(items):  # 批内先去重，保住顺序
                    # 当天口径：今天入过的、今天扫过的（哪怕这条是昨天建的），一律不再进队列。
                    # 业务上「同一天绝不重复揽收」，隔天才允许重新入（退回件重揽）。
                    hit = conn.execute(
                        "SELECT 1 FROM scan_jobs WHERE waybill = ? AND ("
                        "day = ? OR (scanned_at IS NOT NULL AND substr(scanned_at, 1, 10) = ?)) "
                        "LIMIT 1",
                        (waybill, day, day),
                    ).fetchone()
                    if hit:
                        duplicated += 1
                        continue
                    conn.execute(
                        "INSERT INTO scan_jobs"
                        "(waybill, source, source_ref, status, created_at, day) "
                        "VALUES (?, ?, ?, 'pending', ?, ?)",
                        (waybill, source, source_ref, now, day),
                    )
                    inserted += 1
                self._audit(
                    conn, actor, "enqueue",
                    f"source={source} ref={source_ref} inserted={inserted} duplicated={duplicated}",
                )
        log.info(
            "enqueue 完成", extra={"source": source, "status": f"inserted={inserted} dup={duplicated}"}
        )
        return EnqueueResult(total=len(set(items)), inserted=inserted, duplicated=duplicated)

    def import_seen(self, kind: str, ref: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM imports WHERE kind = ? AND ref = ?", (kind, ref)
            ).fetchone()
            return row is not None

    def record_import(
        self, kind: str, ref: str, label: str, total: int, inserted: int, duplicated: int
    ) -> bool:
        """记录一次导入。已存在则返回 False（调用方应跳过整份文件，避免重复入队）。"""
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO imports(kind, ref, label, total, inserted, duplicated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, ref, label, total, inserted, duplicated, utcnow()),
            )
            return cur.rowcount == 1

    # ---------- 批次 ----------

    def active_batch(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE status = 'running' ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def rollover_stale_batch(self, actor: str = "system") -> dict[str, Any] | None:
        """跨天收尾：进行中的批次里若已经没有「当天」的待扫单，就结束它。

        昨天的遗留单会随 abort 退回公共池（仍是 pending，留在库里可查），
        但不会再被任何批次带走，所以手机永远拿不到昨天的单号。
        """
        batch = self.active_batch()
        if not batch:
            return None
        with self._conn() as conn:
            today_n = conn.execute(
                "SELECT COUNT(*) FROM scan_jobs WHERE batch_id = ? AND status = 'pending' AND day = ?",
                (batch["id"], self.today()),
            ).fetchone()[0]
        if today_n:
            return None
        self.set_batch_status(batch["id"], "abort", actor=actor)
        log.info("批次跨天收尾", extra={"batch_id": batch["id"], "status": "rollover"})
        return batch

    def auto_dispatch(self, actor: str = "auto") -> dict[str, Any] | None:
        """自动模式：有新的待扫单、且当前没有进行中的批次，就自动开一批。

        这样人工只需要「启动一次程序」，之后新消息进来自动入队、自动下发，
        手机端拉到任务就能扫，不用再点「开始本批」。
        """
        # 总开关关着 = 完全待机，一条都不发（防止半夜误扫、也方便一键下班）
        if self.rules is not None:
            if not self.rules.running:
                return None
            auto = self.rules.auto_mode
        else:
            # 没有 RuleStore（单测 / 命令行导入）按老逻辑走，只认 auto_mode
            auto = self.config.scan.auto_mode
        if not auto:
            return None
        with self._conn() as conn:
            busy = conn.execute(
                "SELECT id FROM batches WHERE status IN ('running', 'paused') LIMIT 1"
            ).fetchone()
            if busy:
                return None
            pending = conn.execute(
                "SELECT COUNT(*) FROM scan_jobs WHERE status = 'pending' AND day = ?",
                (self.today(),),
            ).fetchone()[0]
        if not pending:
            return None
        # 三重取小：积压量、人工单批上限、申行者单次上限
        size = min(int(pending), self.batch_max, self.session_max)
        return self.start_batch(size, actor=actor, note="auto")

    def start_batch(self, limit: int, actor: str = "operator", note: str = "") -> dict[str, Any]:
        cfg = self.config.scan  # noqa: F841 - 其它字段仍来自静态配置
        if cfg.require_confirm and not actor:
            raise ValidationError("该配置要求人工确认后才能开始批次")
        if limit <= 0:
            raise ValidationError("批次数量必须大于 0")
        if limit > self.batch_max:
            raise BatchLimitError(f"单批最多 {self.batch_max} 条，请求了 {limit} 条")

        batch_id = datetime.now().strftime("B%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        with self._conn() as conn:
            with transaction(conn):
                running = conn.execute(
                    "SELECT id FROM batches WHERE status = 'running' LIMIT 1"
                ).fetchone()
                if running:
                    raise ConflictError(f"已有进行中的批次 {running['id']}，先暂停或结束它")

                # 只捞「当天入库」的单号：昨天的遗留单一律不进新批次，
                # 否则手机第二天会把昨天的单号又扫一遍（退回件除外，隔天重新入队会拿到新 day）。
                rows = conn.execute(
                    "SELECT id FROM scan_jobs WHERE status = 'pending' AND day = ? "
                    "ORDER BY id LIMIT ?",
                    (self.today(), limit),
                ).fetchall()
                if not rows:
                    raise ConflictError("今天还没有待扫单号，先导入 Excel 或等微信群采集")

                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE scan_jobs SET batch_id = ? WHERE id IN ({placeholders})",
                    (batch_id, *ids),
                )
                conn.execute(
                    "INSERT INTO batches(id, status, limit_n, note, created_at) VALUES (?, 'running', ?, ?, ?)",
                    (batch_id, len(ids), note, utcnow()),
                )
                self._audit(conn, actor, "batch.start", f"{batch_id} n={len(ids)}")
        log.info("批次开始", extra={"batch_id": batch_id, "status": f"n={len(ids)}"})
        return self.get_batch(batch_id) or {}

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            batch = dict(row)
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM scan_jobs WHERE batch_id = ? GROUP BY status",
                (batch_id,),
            ).fetchall()
            batch["counts"] = {r["status"]: r["n"] for r in counts}
            return batch

    def set_batch_status(self, batch_id: str, action: str, actor: str = "operator") -> dict[str, Any]:
        if action not in ("pause", "resume", "abort"):
            raise ValidationError(f"不支持的动作：{action}")
        mapping = {"pause": "paused", "resume": "running", "abort": "aborted"}
        target = mapping[action]
        with self._conn() as conn:
            with transaction(conn):
                row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"批次不存在：{batch_id}")
                if row["status"] in ("done", "aborted") and action != "resume":
                    raise ConflictError(f"批次已结束（{row['status']}），不能再改")

                if action == "resume":
                    running = conn.execute(
                        "SELECT id FROM batches WHERE status = 'running' AND id <> ? LIMIT 1",
                        (batch_id,),
                    ).fetchone()
                    if running:
                        raise ConflictError(f"批次 {running['id']} 正在进行中")

                conn.execute(
                    "UPDATE batches SET status = ?, closed_at = ? WHERE id = ?",
                    (target, utcnow() if target in ("done", "aborted") else None, batch_id),
                )
                if action == "abort":
                    # 未扫的退回公共队列，别让它们永远挂在这个批次上
                    conn.execute(
                        "UPDATE scan_jobs SET batch_id = NULL WHERE batch_id = ? AND status = 'pending'",
                        (batch_id,),
                    )
                    conn.execute(
                        "UPDATE scan_jobs SET status = 'fail', last_error = '批次中止' "
                        "WHERE batch_id = ? AND status = 'running'",
                        (batch_id,),
                    )
                self._audit(conn, actor, f"batch.{action}", batch_id)
        log.info("批次状态变更", extra={"batch_id": batch_id, "status": target})
        return self.get_batch(batch_id) or {}

    # ---------- 任务领取 ----------

    def claim_next(self, batch_id: str | None = None) -> dict[str, Any] | None:
        """原子地领取一条任务。没有可领的返回 None（手机端继续轮询即可）。"""
        with self._conn() as conn:
            with transaction(conn):
                if batch_id:
                    batch = conn.execute(
                        "SELECT * FROM batches WHERE id = ?", (batch_id,)
                    ).fetchone()
                    if not batch:
                        raise NotFoundError(f"批次不存在：{batch_id}")
                    if batch["status"] != "running":
                        raise ConflictError(f"批次 {batch_id} 当前是 {batch['status']}，不能领任务")
                else:
                    batch = conn.execute(
                        "SELECT * FROM batches WHERE status = 'running' ORDER BY created_at DESC, id DESC LIMIT 1"
                    ).fetchone()
                    if not batch:
                        return None
                    batch_id = batch["id"]

                row = conn.execute(
                    "SELECT * FROM scan_jobs WHERE batch_id = ? AND status = 'pending' "
                    "ORDER BY id LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if not row:
                    self._maybe_finish(conn, batch_id)
                    return None

                conn.execute(
                    "UPDATE scan_jobs SET status = 'running', attempts = attempts + 1 WHERE id = ?",
                    (row["id"],),
                )
                job = dict(row)
                job["attempts"] = job["attempts"] + 1
                job["status"] = "running"
        log.info("派发任务", extra={"job_id": job["id"], "waybill": job["waybill"], "batch_id": batch_id})
        return job

    def _maybe_finish(self, conn: sqlite3.Connection, batch_id: str) -> None:
        left = conn.execute(
            "SELECT COUNT(*) AS n FROM scan_jobs WHERE batch_id = ? AND status IN ('pending','running')",
            (batch_id,),
        ).fetchone()["n"]
        if left == 0:
            conn.execute(
                "UPDATE batches SET status = 'done', closed_at = ? WHERE id = ? AND status = 'running'",
                (utcnow(), batch_id),
            )
            self._audit(conn, "system", "batch.done", batch_id)

    # ---------- 结果回报 ----------

    def report(
        self,
        job_id: int,
        status: str,
        *,
        raw: str = "",
        error: str = "",
        screenshot: str = "",
        actor: str = "phone",
        final: bool = False,
    ) -> dict[str, Any]:
        if status not in ("ok", "dup", "fail"):
            raise ValidationError(f"结果状态只能是 ok / dup / fail，收到 {status!r}")

        with self._conn() as conn:
            with transaction(conn):
                row = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"任务不存在：{job_id}")
                if row["status"] in ("ok", "dup", "skipped", "dropped") or (
                    row["status"] == "fail" and status == "fail"
                ):
                    # 幂等：手机重发同一个结果，不重复记；
                    # dropped 是人工作废的，也不让迟到的回报把它翻回来；
                    # fail 行收到迟到的 fail 回报同理 —— 不然重试计数没满时
                    # 会被翻回 pending（拦截件就是这么被卡成死循环的）。
                    # fail 行仍允许被 ok/dup 覆盖（人工补扫成功是合理的）。
                    return dict(row)

                now = utcnow()
                if status == "fail":
                    attempts = row["attempts"]
                    # final=True：手机端确定这单永远揽不了（如申行者弹窗移除的
                    # 拦截件），直接落终态，不回 pending。不走它就会死循环：
                    # fail → pending → 再注入 → 再弹窗移除 → 再 fail……
                    retryable = (not final) and attempts < self.max_attempts
                    if retryable:
                        conn.execute(
                            "UPDATE scan_jobs SET status = 'pending', last_error = ?, result_raw = ? WHERE id = ?",
                            (error or "扫描失败", raw, job_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE scan_jobs SET status = 'fail', last_error = ?, result_raw = ?, "
                            "screenshot = ?, scanned_at = ? WHERE id = ?",
                            (error or "扫描失败", raw, screenshot, now, job_id),
                        )
                else:
                    conn.execute(
                        "UPDATE scan_jobs SET status = ?, result_raw = ?, last_error = NULL, "
                        "screenshot = ?, scanned_at = ? WHERE id = ?",
                        (status, raw, screenshot, now, job_id),
                    )
                self._audit(conn, actor, f"job.{status}", f"id={job_id} waybill={row['waybill']}")
                updated = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
                batch_id = updated["batch_id"]
                self._maybe_finish(conn, batch_id)
        log.info(
            "结果回报",
            extra={"job_id": job_id, "waybill": row["waybill"], "status": status},
        )
        return dict(updated)

    def bump_attempt(self, job_id: int) -> None:
        """补记一次尝试。

        批量下发（/api/phone/next 按批次拉文本）不走 claim_next，attempts
        一直是 0；不补的话 report(fail) 里 ``attempts < max_attempts`` 永远成立，
        失败单永远回 pending、永远落不了终态、批次永远收不了口。
        """
        with self._conn() as conn:
            with transaction(conn):
                conn.execute(
                    "UPDATE scan_jobs SET attempts = attempts + 1 WHERE id = ?", (job_id,)
                )

    def retry(self, job_id: int, actor: str = "operator") -> dict[str, Any]:
        with self._conn() as conn:
            with transaction(conn):
                row = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"任务不存在：{job_id}")
                if row["status"] == "ok":
                    raise ConflictError("已成功的任务不重试，避免重复产生物流轨迹")
                conn.execute(
                    "UPDATE scan_jobs SET status = 'pending', last_error = NULL, attempts = 0 WHERE id = ?",
                    (job_id,),
                )
                self._audit(conn, actor, "job.retry", f"id={job_id}")
                return dict(conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone())

    def clear_retry(self, *, day: str | None = None, actor: str = "operator") -> dict[str, Any]:
        """一键清掉「可重试」的任务（fail + skipped），全部置为终态。

        为什么需要：手机被两个程序抢的时候，一轮下来可能留下几十上百条
        skipped，界面上每行都有「重试」按钮，一条条点根本不现实，而且点错一条
        就会重复上传产生多余物流轨迹。这里给一个批量口子。

        和 retry 的区别：
          · retry       → 置回 pending，等下一轮重新下发手机
          · clear_retry → 置为 dropped（终态），不再出现在可重试列表

        默认只处理当天；已 ok / dup 的一律不动（防止重复产生轨迹）。
        """
        target_day = day or self.today()
        now = utcnow()
        with self._conn() as conn:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT id FROM scan_jobs WHERE status IN ('fail', 'skipped') AND day = ?",
                    (target_day,),
                ).fetchall()
                if not rows:
                    return {"cleared": 0, "day": target_day}
                cur = conn.execute(
                    "UPDATE scan_jobs SET status = 'dropped', last_error = ?, scanned_at = ? "
                    "WHERE status IN ('fail', 'skipped') AND day = ?",
                    ("人工清除重试", now, target_day),
                )
                cleared = cur.rowcount if cur.rowcount >= 0 else len(rows)
                self._audit(
                    conn, actor, "jobs.clear_retry", f"day={target_day} n={cleared}"
                )
                return {"cleared": cleared, "day": target_day}

    def request_run_now(self, *, actor: str = "operator") -> dict[str, Any]:
        """界面上点「立即抓取」：给监听进程留一句话，让它马上跑一轮。

        为什么不让后端自己去操作手机：后端（`miao-core.exe`）是打包产物，
        里面**没有** `tools/` 下的 adb 脚本；而且后端是常驻 HTTP 服务，
        在请求线程里跑几分钟的手机流程会把界面卡死。

        所以这里只做「留话」，真正的执行还是 `tools/phone_watch.py` 干：
        它每 10s 查一次队列，顺带看一眼这个标记，看到就立刻跑。
        和对方网关的 `/watch-now` 是同一个思路 —— 手机侧没有常驻服务端，
        电脑唯一能做的就是留话等它来取。
        """
        now = utcnow()
        with self._conn() as conn:
            with transaction(conn):
                # 值 = 「时间戳#序号」。为什么不能只用时间戳：utcnow() 精度到秒，
                # 连点两下会落在同一秒里，第二个请求会被当成「跟上次一模一样」
                # 而永远不被消费（监听以为已经处理过了）—— 这是测试当场抓出来的。
                prev = get_runtime(conn, "run.requested_at", "")
                try:
                    seq = int(prev.rsplit("#", 1)[1]) + 1 if "#" in prev else 1
                except (ValueError, IndexError):
                    seq = 1
                stamp = f"{now}#{seq}"
                set_runtime(conn, "run.requested_at", stamp)
                self._audit(conn, actor, "run.request_now", f"at={stamp}")
        log.info("收到「立即抓取」请求，等监听进程接单")
        return {"requested_at": stamp, "note": "监听进程会在几秒内接单"}

    def take_run_request(self, *, actor: str = "phone") -> str:
        """监听进程取单：返回新请求的时间戳，没有新请求就返回空串。

        取值即消费（把 requested_at 清空），保证同一个请求只跑一轮 ——
        否则监听进程会一直读到它、一轮接一轮地跑。
        """
        with self._conn() as conn:
            with transaction(conn):
                want = get_runtime(conn, "run.requested_at", "")
                if not want:
                    return ""
                done = get_runtime(conn, "run.served_at", "")
                if want == done:
                    return ""
                set_runtime(conn, "run.served_at", want)
                self._audit(conn, actor, "run.served", f"at={want}")
                return want

    def run_request_pending(self) -> bool:
        """界面上用来显示「已请求，等监听接单」。"""
        with self._conn() as conn:
            want = get_runtime(conn, "run.requested_at", "")
            if not want:
                return False
            return want != get_runtime(conn, "run.served_at", "")

    def skip(self, job_id: int, reason: str = "", actor: str = "operator") -> dict[str, Any]:
        with self._conn() as conn:
            with transaction(conn):
                row = conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"任务不存在：{job_id}")
                conn.execute(
                    "UPDATE scan_jobs SET status = 'skipped', last_error = ?, scanned_at = ? WHERE id = ?",
                    (reason or "人工跳过", utcnow(), job_id),
                )
                self._audit(conn, actor, "job.skip", f"id={job_id} reason={reason}")
                return dict(conn.execute("SELECT * FROM scan_jobs WHERE id = ?", (job_id,)).fetchone())

    def clear_pending(self, *, day: str | None = None, actor: str = "operator") -> dict[str, Any]:
        """清空待扫队列：只把状态改成 skipped，单号记录保留可查、可单条重试。

        默认只清当天（跨天的历史待扫一律不动）。清完后批次里若已无待扫/扫描中的单，
        批次会被正常收尾，不会挂着一个空批次。
        """
        target_day = day or self.today()
        now = utcnow()
        with self._conn() as conn:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT id, batch_id FROM scan_jobs WHERE status = 'pending' AND day = ?",
                    (target_day,),
                ).fetchall()
                if not rows:
                    return {"cleared": 0, "day": target_day, "batches": []}
                cur = conn.execute(
                    "UPDATE scan_jobs SET status = 'skipped', last_error = ?, scanned_at = ? "
                    "WHERE status = 'pending' AND day = ?",
                    ("人工清除待扫", now, target_day),
                )
                cleared = cur.rowcount if cur.rowcount >= 0 else len(rows)
                batch_ids = sorted({str(r["batch_id"]) for r in rows if r["batch_id"]})
                for batch_id in batch_ids:
                    self._maybe_finish(conn, batch_id)
                    # 暂停中的批次不会被 _maybe_finish 收尾，这里一并结束掉，别挂个空批次
                    conn.execute(
                        "UPDATE batches SET status = 'done', closed_at = ? "
                        "WHERE id = ? AND status = 'paused'",
                        (now, batch_id),
                    )
                self._audit(
                    conn, actor, "jobs.clear_pending", f"day={target_day} n={cleared}"
                )
                return {"cleared": cleared, "day": target_day, "batches": batch_ids}

    # ---------- 查询 ----------

    def list_jobs(
        self,
        *,
        status: str | None = None,
        batch_id: str | None = None,
        day: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scan_jobs WHERE 1=1"
        params: list[Any] = []
        if status:
            if status not in VALID_STATUS:
                raise ValidationError(f"未知状态：{status}")
            sql += " AND status = ?"
            params.append(status)
        if batch_id:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        if day:
            sql += " AND day = ?"
            params.append(day)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    @staticmethod
    def today() -> str:
        """当地日期 YYYY-MM-DD（utcnow 带时区偏移，前 10 位就是当地日期）。"""
        return utcnow()[:10]

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            counts = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM scan_jobs GROUP BY status"
                ).fetchall()
            }
            batch = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            hb_at = get_runtime(conn, "phone.last_seen", "")
            hb_page = get_runtime(conn, "phone.page", "")
            total = sum(counts.values())
            done = counts.get("ok", 0) + counts.get("dup", 0)
            today = self.today()
            t = conn.execute(
                "SELECT "
                "SUM(CASE WHEN substr(scanned_at,1,10)=? AND status='ok' THEN 1 ELSE 0 END) AS ok,"
                "SUM(CASE WHEN substr(scanned_at,1,10)=? AND status='dup' THEN 1 ELSE 0 END) AS dup,"
                "SUM(CASE WHEN substr(scanned_at,1,10)=? AND status='fail' THEN 1 ELSE 0 END) AS fail,"
                "SUM(CASE WHEN day=? THEN 1 ELSE 0 END) AS enqueued,"
                "SUM(CASE WHEN day=? AND status='pending' THEN 1 ELSE 0 END) AS pend "
                "FROM scan_jobs",
                (today, today, today, today, today),
            ).fetchone()
            return {
                "today": {
                    "date": today,
                    "ok": int(t["ok"] or 0),
                    "dup": int(t["dup"] or 0),
                    "fail": int(t["fail"] or 0),
                    "enqueued": int(t["enqueued"] or 0),
                    "pending": int(t["pend"] or 0),
                },
                "counts": counts,
                "total": total,
                "done": done,
                "pending": counts.get("pending", 0),
                # 界面上「立即抓取」按钮要据此判断监听有没有接单
                "run_pending": (
                    get_runtime(conn, "run.requested_at", "")
                    != get_runtime(conn, "run.served_at", "")
                ),
                "batch": dict(batch) if batch else None,
                "phone": {"last_seen": hb_at, "page": hb_page},
                "service": {
                    "running": bool(self.rules.running) if self.rules is not None else False,
                    "group_name": self.rules.group_name if self.rules is not None else "",
                    "auto_mode": bool(self.rules.auto_mode) if self.rules is not None else False,
                    "window": self.rules.window[0] if self.rules is not None else "today",
                },
            }

    def daily_stats(self, days: int = 14) -> list[dict[str, Any]]:
        """按天汇总：入库数按 day 列，揽收结果按 scanned_at 的日期，两边对齐到同一天。"""
        days = max(1, min(int(days), 90))
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT d AS date, SUM(enq) AS enqueued, SUM(ok) AS ok,"
                " SUM(dup) AS dup, SUM(fail) AS fail FROM ("
                "  SELECT day AS d, 1 AS enq, 0 AS ok, 0 AS dup, 0 AS fail"
                "    FROM scan_jobs WHERE day IS NOT NULL"
                "  UNION ALL"
                "  SELECT substr(scanned_at, 1, 10) AS d, 0 AS enq,"
                "    CASE WHEN status='ok' THEN 1 ELSE 0 END AS ok,"
                "    CASE WHEN status='dup' THEN 1 ELSE 0 END AS dup,"
                "    CASE WHEN status='fail' THEN 1 ELSE 0 END AS fail"
                "    FROM scan_jobs WHERE scanned_at IS NOT NULL"
                ") GROUP BY d ORDER BY d DESC LIMIT ?",
                (days,),
            ).fetchall()
        return [
            {
                "date": r["date"],
                "enqueued": int(r["enqueued"] or 0),
                "ok": int(r["ok"] or 0),
                "dup": int(r["dup"] or 0),
                "fail": int(r["fail"] or 0),
            }
            for r in rows
        ]

    def heartbeat(self, page: str = "", battery: str = "") -> None:
        with self._conn() as conn:
            set_runtime(conn, "phone.last_seen", utcnow())
            if page:
                set_runtime(conn, "phone.page", page)
            if battery:
                set_runtime(conn, "phone.battery", battery)
