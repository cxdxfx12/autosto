from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scan.config import Config, ExcelConfig, ScanConfig, ServerConfig, WeChatConfig  # noqa: E402
from scan.db import migrate  # noqa: E402
from scan.errors import BatchLimitError, ConflictError, ValidationError  # noqa: E402
from scan.service import JobService  # noqa: E402
from scan.waybill import extract, extract_with_rejects, normalize_scalar  # noqa: E402

PREFIXES = ("77",)
LENGTHS = (12, 13, 15)


def make_config(tmp: Path) -> Config:
    return Config(
        root=tmp,
        db_path=tmp / "scan.db",
        log_dir=tmp / "logs",
        server=ServerConfig(host="127.0.0.1", port=0, token="x" * 24, allow_lan=False),
        scan=ScanConfig(
            prefixes=PREFIXES, lengths=LENGTHS, regex="", require_confirm=True,
            batch_max=3, max_attempts=2, poll_interval_ms=1000,
        ),
        excel=ExcelConfig(inbox_dir=tmp / "inbox"),
        wechat=WeChatConfig(enabled=False, group_name="", poll_ms=1000, max_messages=10),
    )


class WaybillTest(unittest.TestCase):
    def test_plain(self) -> None:
        self.assertEqual(extract("7712345678901", PREFIXES, LENGTHS), ["7712345678901"])

    def test_excel_float(self) -> None:
        text, warning = normalize_scalar(7712345678901.0)
        self.assertEqual(text, "7712345678901")
        self.assertIsNone(warning)

    def test_long_float_warns(self) -> None:
        text, warning = normalize_scalar(7.712345678901234e14)
        self.assertIn("精度", warning or "")
        self.assertTrue(text.startswith("771234567890123"))

    def test_separators_between_digits(self) -> None:
        self.assertEqual(
            extract("单号 7712-3456-78901 已发", PREFIXES, LENGTHS), ["7712345678901"]
        )

    def test_does_not_join_across_words(self) -> None:
        # 不能把「订单 77 ... 备注 88」拼成一个号
        self.assertEqual(extract("订单 77 备注 88", PREFIXES, LENGTHS), [])

    def test_fullwidth(self) -> None:
        self.assertEqual(extract("７７１２３４５６７８９０１", PREFIXES, LENGTHS), ["7712345678901"])

    def test_multiple_and_dedupe(self) -> None:
        text = "7712345678901 7712345678901 7798765432109"
        self.assertEqual(extract(text, PREFIXES, LENGTHS), ["7712345678901", "7798765432109"])

    def test_wrong_prefix_rejected_with_reason(self) -> None:
        hits, rejects = extract_with_rejects("8812345678901", PREFIXES, LENGTHS)
        self.assertEqual(hits, [])
        self.assertEqual(len(rejects), 1)
        self.assertIn("前缀", rejects[0].reason)

    def test_message_in_group_chat(self) -> None:
        text = "@。zz 麻烦揽收下 7712345678901 7712345678902 谢谢"
        self.assertEqual(extract(text, PREFIXES, LENGTHS), ["7712345678901", "7712345678902"])


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        migrate(self.config.db_path)
        self.svc = JobService(self.config.db_path, self.config)

    def test_enqueue_dedupe(self) -> None:
        r1 = self.svc.enqueue(["7712345678901", "7712345678902"], source="excel")
        self.assertEqual((r1.inserted, r1.duplicated), (2, 0))
        r2 = self.svc.enqueue(["7712345678901"], source="excel")
        self.assertEqual((r2.inserted, r2.duplicated), (0, 1))

    def test_enqueue_dedupe_is_per_day(self) -> None:
        """当天绝不重复入队；隔天允许重新入（退回件要重揽）。"""
        num = "7712345678001"
        self.assertEqual(self.svc.enqueue([num], source="wechat").inserted, 1)
        self.assertEqual(self.svc.enqueue([num], source="wechat").duplicated, 1)

        # 今天扫过（ok）的，当天再发也不许入队
        job = self.svc.claim_next()
        self.assertIsNone(job)  # 没开批次时取不到，直接按批开
        batch = self.svc.start_batch(1, note="test")
        job = self.svc.claim_next()
        self.svc.report(job["id"], "ok", raw="揽收成功")
        self.assertEqual(self.svc.enqueue([num], source="wechat").duplicated, 1)

        # 把这条改成「昨天建的」，今天就能重新入队
        import sqlite3

        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute(
                "UPDATE scan_jobs SET day = '2000-01-01', created_at = '2000-01-01T09:00:00+08:00', "
                "scanned_at = '2000-01-01T09:10:00+08:00' WHERE waybill = ?",
                (num,),
            )
        again = self.svc.enqueue([num], source="wechat")
        self.assertEqual((again.inserted, again.duplicated), (1, 0))
        self.assertTrue(batch["id"])

    def test_stats_today(self) -> None:
        """今日统计按天走：入库看 day 列，揽收看 scanned_at 的日期。"""
        self.svc.enqueue(
            ["7712345678101", "7712345678102", "7712345678103"], source="excel"
        )
        s = self.svc.stats()
        self.assertEqual(s["today"]["date"], self.svc.today())
        self.assertEqual(s["today"]["enqueued"], 3)
        self.assertEqual(s["today"]["pending"], 3)
        self.assertEqual(s["today"]["ok"], 0)

        self.svc.start_batch(2, note="test")
        job = self.svc.claim_next()
        self.svc.report(job["id"], "ok", raw="揽收成功")
        s2 = self.svc.stats()
        self.assertEqual(s2["today"]["ok"], 1)
        self.assertEqual(s2["today"]["pending"], 2)
        self.assertEqual(s2["today"]["enqueued"], 3)

    def test_daily_stats(self) -> None:
        """按天汇总：入库与揽收结果对齐到同一天。"""
        self.svc.enqueue(["7712345678201", "7712345678202"], source="excel")
        self.svc.start_batch(2, note="test")
        job = self.svc.claim_next()
        self.svc.report(job["id"], "ok", raw="揽收成功")
        rows = self.svc.daily_stats(7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], self.svc.today())
        self.assertEqual(rows[0]["enqueued"], 2)
        self.assertEqual(rows[0]["ok"], 1)
        self.assertEqual(rows[0]["fail"], 0)

    def test_batch_only_takes_today(self) -> None:
        """昨天的待扫单号不许进新批次，手机也就永远拿不到。"""
        import sqlite3

        self.svc.enqueue(["7712345678301", "7712345678302"], source="excel")
        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute(
                "UPDATE scan_jobs SET day = '2000-01-01' WHERE waybill = '7712345678301'"
            )
        batch = self.svc.start_batch(2, note="test")
        with sqlite3.connect(self.config.db_path) as conn:
            got = [
                r[0]
                for r in conn.execute(
                    "SELECT waybill FROM scan_jobs WHERE batch_id = ? ORDER BY waybill",
                    (batch["id"],),
                )
            ]
        self.assertEqual(got, ["7712345678302"])

    def test_rollover_stale_batch(self) -> None:
        """批次里只剩昨天的单时自动收尾，昨天的单退回池但不再下发。"""
        import sqlite3

        self.svc.enqueue(["7712345678401"], source="excel")
        batch = self.svc.start_batch(1, note="test")
        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute("UPDATE scan_jobs SET day = '2000-01-01'")
        closed = self.svc.rollover_stale_batch()
        self.assertEqual(closed["id"], batch["id"])
        self.assertIsNone(self.svc.active_batch())
        with sqlite3.connect(self.config.db_path) as conn:
            row = conn.execute(
                "SELECT status, batch_id FROM scan_jobs WHERE waybill = '7712345678401'"
            ).fetchone()
        self.assertEqual(row, ("pending", None))

    def test_clear_pending_only_today(self) -> None:
        """清除待扫只清当天，昨天的单原样保留，批次自动收尾。"""
        import sqlite3

        self.svc.enqueue(["7712345678501", "7712345678502"], source="excel")
        batch = self.svc.start_batch(2, note="test")
        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute("UPDATE scan_jobs SET day = '2000-01-01' WHERE waybill = '7712345678501'")
        res = self.svc.clear_pending()
        self.assertEqual(res["cleared"], 1)
        self.assertEqual(res["day"], self.svc.today())
        with sqlite3.connect(self.config.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = {
                r["waybill"]: (r["status"], r["batch_id"])
                for r in conn.execute("SELECT waybill, status, batch_id FROM scan_jobs")
            }
            bstatus = conn.execute(
                "SELECT status FROM batches WHERE id = ?", (batch["id"],)
            ).fetchone()[0]
        self.assertEqual(rows["7712345678502"], ("skipped", batch["id"]))
        self.assertEqual(rows["7712345678501"], ("pending", batch["id"]))
        self.assertEqual(bstatus, "running")  # 昨天的单还挂在批次里，不能收尾

    def test_clear_retry_only_today_and_skips_ok(self) -> None:
        """清除重试：当天 fail/skipped 置 dropped，ok 与昨天的单绝不动。"""
        import sqlite3

        self.svc.enqueue(
            ["7712345678601", "7712345678602", "7712345678603"], source="excel"
        )
        self.svc.start_batch(3, note="test")

        def fail_to_end(waybill: str) -> None:
            """反复取单+报失败，直到 attempts 用尽变成终态 fail。"""
            for _ in range(self.svc.max_attempts):
                job = self.svc.claim_next()
                assert job and job["waybill"] == waybill, job
                self.svc.report(job["id"], "fail", raw="扫描异常")

        job = self.svc.claim_next()
        assert job
        self.svc.report(job["id"], "ok", raw="揽收成功")
        fail_to_end("7712345678602")
        fail_to_end("7712345678603")
        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute(
                "UPDATE scan_jobs SET day = '2000-01-01' WHERE waybill = '7712345678603'"
            )

        res = self.svc.clear_retry()
        self.assertEqual(res["cleared"], 1)  # 只清当天那 1 条 fail
        self.assertEqual(res["day"], self.svc.today())
        with sqlite3.connect(self.config.db_path) as conn:
            rows = {
                r[0]: r[1]
                for r in conn.execute("SELECT waybill, status FROM scan_jobs")
            }
        self.assertEqual(rows["7712345678601"], "ok")
        self.assertEqual(rows["7712345678602"], "dropped")
        self.assertEqual(rows["7712345678603"], "fail")  # 昨天的不动

        # 再清一次：今天的已经没了，不能重复计数
        self.assertEqual(self.svc.clear_retry()["cleared"], 0)

    def test_dropped_is_final_against_late_report(self) -> None:
        """作废之后，手机迟到的回报不能把它翻回 ok。"""
        import sqlite3

        self.svc.enqueue(["7712345678701"], source="excel")
        self.svc.start_batch(1, note="test")
        last = None
        for _ in range(self.svc.max_attempts):
            job = self.svc.claim_next()
            assert job
            last = self.svc.report(job["id"], "fail", raw="扫描异常")
        assert last and last["status"] == "fail", last
        self.svc.clear_retry()
        # 手机这会儿才把成功的回报送来
        again = self.svc.report(last["id"], "ok", raw="揽收成功")
        self.assertEqual(again["status"], "dropped")
        with sqlite3.connect(self.config.db_path) as conn:
            status = conn.execute(
                "SELECT status FROM scan_jobs WHERE id = ?", (last["id"],)
            ).fetchone()[0]
        self.assertEqual(status, "dropped")

    def test_run_now_signal_consumed_once(self) -> None:
        """「立即抓取」：留话 → 取一次就跑，不能取两次跑两轮。"""
        self.assertFalse(self.svc.run_request_pending())
        self.assertEqual(self.svc.take_run_request(), "")   # 没请求时取不到

        res = self.svc.request_run_now()
        self.assertTrue(res["requested_at"])
        self.assertTrue(self.svc.run_request_pending())

        got = self.svc.take_run_request()
        self.assertEqual(got, res["requested_at"])
        self.assertFalse(self.svc.run_request_pending())
        # 关键：第二次取必须是空 —— 否则监听会一轮接一轮地跑
        self.assertEqual(self.svc.take_run_request(), "")

    def test_run_now_twice_both_take_effect(self) -> None:
        """连点两下：第二下也要算数（靠时间戳而不是布尔）。"""
        import time

        first = self.svc.request_run_now()["requested_at"]
        self.svc.take_run_request()
        time.sleep(0.01)
        second = self.svc.request_run_now()["requested_at"]
        self.assertNotEqual(first, second)
        self.assertTrue(self.svc.run_request_pending())
        self.assertEqual(self.svc.take_run_request(), second)

    def test_stats_reports_run_pending(self) -> None:
        """界面靠 stats.run_pending 判断监听接没接单。"""
        self.assertFalse(self.svc.stats()["run_pending"])
        self.svc.request_run_now()
        self.assertTrue(self.svc.stats()["run_pending"])
        self.svc.take_run_request()
        self.assertFalse(self.svc.stats()["run_pending"])

    def test_claim_requires_batch(self) -> None:
        self.svc.enqueue(["7712345678901"], source="excel")
        self.assertIsNone(self.svc.claim_next())

    def test_batch_limit(self) -> None:
        self.svc.enqueue([f"77123456789{i:02d}" for i in range(5)], source="excel")
        with self.assertRaises(BatchLimitError):
            self.svc.start_batch(4)

    def test_batch_flow(self) -> None:
        self.svc.enqueue(["7712345678901", "7712345678902"], source="excel")
        batch = self.svc.start_batch(2)
        self.assertEqual(batch["limit_n"], 2)
        job = self.svc.claim_next()
        self.assertIsNotNone(job)
        assert job
        self.assertEqual(job["status"], "running")
        done = self.svc.report(job["id"], "ok", raw="揽收成功")
        self.assertEqual(done["status"], "ok")
        self.svc.claim_next()
        self.svc.report(2, "ok", raw="揽收成功")
        self.assertEqual(self.svc.get_batch(batch["id"])["status"], "done")

    def test_fail_retries_then_fails(self) -> None:
        self.svc.enqueue(["7712345678901"], source="excel")
        self.svc.start_batch(1)
        job = self.svc.claim_next()
        assert job
        after = self.svc.report(job["id"], "fail", error="超时")
        self.assertEqual(after["status"], "pending")  # max_attempts=2，还能再试一次
        job2 = self.svc.claim_next()
        assert job2
        after2 = self.svc.report(job2["id"], "fail", error="超时")
        self.assertEqual(after2["status"], "fail")

    def test_final_fail_skips_retry(self) -> None:
        """final=True 的 fail（如拦截件被申行者弹窗移除）一次就落终态。

        不走它就会死循环：fail 回 pending → 下一轮又被注入 → 又被弹窗移除
        → 又 fail……批次永远收不了口（实测 2026-10-26 卡死过）。
        """
        self.svc.enqueue(["7712345678901"], source="excel")
        self.svc.start_batch(1)
        job = self.svc.claim_next()
        assert job
        after = self.svc.report(job["id"], "fail", error="拦截", final=True)
        self.assertEqual(after["status"], "fail")
        self.assertEqual(after["last_error"], "拦截")
        # 终态之后迟到的回报也不能把它翻回 pending
        again = self.svc.report(job["id"], "fail", error="迟到的回报")
        self.assertEqual(again["status"], "fail")

    def test_report_idempotent(self) -> None:
        self.svc.enqueue(["7712345678901"], source="excel")
        self.svc.start_batch(1)
        job = self.svc.claim_next()
        assert job
        self.svc.report(job["id"], "ok", raw="揽收成功")
        again = self.svc.report(job["id"], "fail", error="迟到的重复回报")
        self.assertEqual(again["status"], "ok")

    def test_two_running_batches_forbidden(self) -> None:
        self.svc.enqueue(["7712345678901", "7712345678902"], source="excel")
        self.svc.start_batch(1)
        with self.assertRaises(ConflictError):
            self.svc.start_batch(1)

    def test_abort_returns_rest_to_pool(self) -> None:
        self.svc.enqueue(["7712345678901", "7712345678902"], source="excel")
        batch = self.svc.start_batch(2)
        self.svc.set_batch_status(batch["id"], "abort")
        jobs = self.svc.list_jobs(status="pending")
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j["batch_id"] is None for j in jobs))

    def test_skip_and_retry(self) -> None:
        self.svc.enqueue(["7712345678901"], source="excel")
        job = self.svc.list_jobs()[0]
        self.svc.skip(job["id"], "测试")
        self.assertEqual(self.svc.list_jobs()[0]["status"], "skipped")
        self.svc.retry(job["id"])
        self.assertEqual(self.svc.list_jobs()[0]["status"], "pending")

    def test_require_confirm_rejects_empty_actor(self) -> None:
        self.svc.enqueue(["7712345678901"], source="excel")
        with self.assertRaises(ValidationError):
            self.svc.start_batch(1, actor="")


class ExcelTest(unittest.TestCase):
    def test_extract_from_workbook(self) -> None:
        from openpyxl import Workbook

        from scan.extract import extract_file

        tmp = Path(tempfile.mkdtemp())
        path = tmp / "在途.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "10.16"
        ws["A1"] = "序号"
        ws["B1"] = "运单号"
        ws["A2"] = 1
        ws["B2"] = 7712345678901  # 数字格式，最常见的坑
        ws["B3"] = " 7712345678902 "
        ws["B4"] = "7712-3456-78903"
        ws["C4"] = "备注：8812345678901 不是申通"
        ws2 = wb.create_sheet("补录")
        ws2["A1"] = "7712345678904"
        wb.save(path)

        result = extract_file(path, PREFIXES, LENGTHS)
        self.assertEqual(
            sorted(result.waybills),
            sorted(["7712345678901", "7712345678902", "7712345678903", "7712345678904"]),
        )
        self.assertTrue(any("10.16!B2" in h.location for h in result.hits))


class AutoDispatchTest(unittest.TestCase):
    """自动模式：采集到新单就自动开批下发，人工只启动一次。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        migrate(self.config.db_path)
        self.svc = JobService(self.config.db_path, self.config)

    def test_no_jobs_no_batch(self) -> None:
        self.assertIsNone(self.svc.auto_dispatch())

    def test_dispatch_when_pending(self) -> None:
        self.svc.enqueue(["7712345678901", "7712345678902"], source="wx")
        batch = self.svc.auto_dispatch()
        self.assertIsNotNone(batch)
        self.assertEqual(batch["limit_n"], 2)
        # 已经在跑，不该再开第二批
        self.assertIsNone(self.svc.auto_dispatch())

    def test_respects_batch_max(self) -> None:
        self.svc.enqueue([f"77123456789{i:02d}" for i in range(10)], source="wx")
        batch = self.svc.auto_dispatch()
        self.assertEqual(batch["limit_n"], self.config.scan.batch_max)

    def test_off_switch(self) -> None:
        import dataclasses

        from scan.rules import RuleStore

        rules = RuleStore(self.config, config_path=self.tmp / "config.yaml")
        rules.update_limits(10, 2, auto_mode=False)
        svc = JobService(self.config.db_path, dataclasses.replace(
            self.config), rules)
        svc.enqueue(["7712345678903"], source="wx")
        self.assertIsNone(svc.auto_dispatch())


class SplitBatchesTest(unittest.TestCase):
    """申行者单次最多 500 条：超过要自动拆成多批，依次扫。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        import dataclasses

        base = make_config(self.tmp)
        self.config = dataclasses.replace(
            base,
            scan=dataclasses.replace(base.scan, batch_max=500, session_max=500),
        )
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        migrate(self.config.db_path)
        self.svc = JobService(self.config.db_path, self.config)

    def _finish(self, batch_id: str) -> None:
        with self.svc._conn() as conn:  # noqa: SLF001
            conn.execute("UPDATE scan_jobs SET status='ok' WHERE batch_id=?", (batch_id,))
            conn.execute("UPDATE batches SET status='done' WHERE id=?", (batch_id,))

    def test_split_into_500_and_rest(self) -> None:
        numbers = [f"77{i:013d}" for i in range(1200)]
        self.svc.enqueue(numbers, source="excel")
        first = self.svc.auto_dispatch()
        self.assertEqual(first["limit_n"], 500)      # 不会被 1200 冲垮

        self._finish(first["id"])
        second = self.svc.auto_dispatch()
        self.assertEqual(second["limit_n"], 500)

        self._finish(second["id"])
        third = self.svc.auto_dispatch()
        self.assertEqual(third["limit_n"], 200)      # 剩下的 200 单独一批

        self._finish(third["id"])
        self.assertIsNone(self.svc.auto_dispatch())   # 扫完不再开空批

    def test_smaller_limit_wins(self) -> None:
        """人工把单批上限设成 100，就不能发 500。"""
        import dataclasses

        cfg = dataclasses.replace(
            self.config, scan=dataclasses.replace(self.config.scan, batch_max=100)
        )
        svc = JobService(cfg.db_path, cfg)
        svc.enqueue([f"78{i:013d}" for i in range(300)], source="excel")
        self.assertEqual(svc.auto_dispatch()["limit_n"], 100)


if __name__ == "__main__":
    unittest.main()
