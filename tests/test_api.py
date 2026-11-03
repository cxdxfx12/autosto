from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scan.db import migrate  # noqa: E402
from scan.rules import RuleStore  # noqa: E402
from scan.server import make_server  # noqa: E402
from tests.test_core import make_config  # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        cls.config = make_config(cls.tmp)
        migrate(cls.config.db_path)
        # config_path 指到临时目录，避免测试把规则写进真实的 config.yaml
        cls.rules = RuleStore(cls.config, config_path=cls.tmp / "config.yaml")
        cls.httpd = make_server(cls.config, cls.rules)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "X-Token": self.config.server.token},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health(self) -> None:
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_ready(self) -> None:
        status, body = self.call("GET", "/ready")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_flow(self) -> None:
        from openpyxl import Workbook

        path = self.tmp / "api.xlsx"
        wb = Workbook()
        ws = wb.active
        for i in range(3):
            ws.cell(row=i + 1, column=1, value=f"77123456789{i:02d}")
        wb.save(path)

        status, body = self.call("POST", "/api/imports/excel", {"path": str(path)})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["inserted"], 3)

        # 同一份文件再来一次：内容哈希一致，整份跳过
        status, body = self.call("POST", "/api/imports/excel", {"path": str(path)})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["skipped"])

        status, body = self.call("POST", "/api/batches", {"limit": 2})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["batch"]["limit_n"], 2)

        status, body = self.call("GET", "/api/jobs/next")
        self.assertEqual(status, 200)
        job = body["job"]
        self.assertIsNotNone(job)

        status, body = self.call("POST", f"/api/jobs/{job['id']}/result", {"status": "ok", "raw": "揽收成功"})
        self.assertEqual(status, 200)
        self.assertEqual(body["job"]["status"], "ok")

    def test_unknown_route(self) -> None:
        status, body = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_bad_result_status(self) -> None:
        status, body = self.call("POST", "/api/jobs/999999/result", {"status": "wat"})
        self.assertIn(status, (400, 404))
        self.assertIn("error", body)

    def test_index_page(self) -> None:
        with urllib.request.urlopen(self.base + "/", timeout=5) as res:
            html = res.read().decode()
        self.assertIn("运单自动采集自动揽收助手", html)
        self.assertIn("杭州喵喵至家网络有限公司", html)

    def test_scan_rules_get_and_set(self) -> None:
        status, body = self.call("GET", "/api/settings/scan")
        self.assertEqual(status, 200)
        self.assertIn("prefixes", body["rules"])

        status, body = self.call(
            "POST", "/api/settings/scan", {"prefixes": "77, 621", "lengths": "12,13,15"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"]["prefixes"], ["77", "621"])
        self.assertEqual(body["rules"]["lengths"], [12, 13, 15])

        # 再读一次，确认已生效
        status, body = self.call("GET", "/api/settings/scan")
        self.assertEqual(body["rules"]["prefixes"], ["77", "621"])

    def test_scan_rules_reject_bad_input(self) -> None:
        status, body = self.call(
            "POST", "/api/settings/scan", {"prefixes": "abc", "lengths": "15"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, _ = self.call("POST", "/api/settings/scan", {"prefixes": "", "lengths": ""})
        self.assertEqual(status, 400)


    def test_wechat_group_get_and_set(self) -> None:
        status, body = self.call("POST", "/api/settings/wechat",
                                 {"group_name": "自动揽收测试群", "enabled": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"]["group_name"], "自动揽收测试群")
        self.assertTrue(body["rules"]["enabled"])

        # 群名会随 scan 规则一起返回，界面只调一个接口
        status, body = self.call("GET", "/api/settings/scan")
        self.assertEqual(body["rules"]["group_name"], "自动揽收测试群")

    def test_wechat_group_missing_field(self) -> None:
        status, body = self.call("POST", "/api/settings/wechat", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_wechat_enabled_must_be_bool(self) -> None:
        status, _ = self.call("POST", "/api/settings/wechat",
                              {"group_name": "x", "enabled": "yes"})
        self.assertEqual(status, 400)


    def test_settings_center_payload(self) -> None:
        status, body = self.call("GET", "/api/settings")
        self.assertEqual(status, 200)
        s = body["settings"]
        for key in ("prefixes", "lengths", "group_name", "enabled", "window",
                    "batch_max", "max_attempts"):
            self.assertIn(key, s)
        self.assertIn("db", body["paths"])

    def test_limits_update_and_apply(self) -> None:
        status, body = self.call("POST", "/api/settings/limits",
                                 {"batch_max": 5, "max_attempts": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"]["batch_max"], 5)
        # 服务立刻用新上限：请求 6 条应被拒
        status, body = self.call("POST", "/api/batches", {"limit": 6})
        self.assertIn(status, (400, 409))
        self.assertIn("单批最多 5 条", body["error"]["message"])
        # 必须还原：RuleStore 是类级共享的，改完不还会把后面所有
        # 「失败先回 pending 重试」的测试带成 max_attempts=1
        status, _ = self.call("POST", "/api/settings/limits",
                              {"batch_max": 500, "max_attempts": 2})
        self.assertEqual(status, 200)

    def test_limits_validation(self) -> None:
        status, _ = self.call("POST", "/api/settings/limits",
                              {"batch_max": 0, "max_attempts": 1})
        self.assertEqual(status, 400)
        status, _ = self.call("POST", "/api/settings/limits",
                              {"batch_max": 5, "max_attempts": 99})
        self.assertEqual(status, 400)

    def test_preview_uses_editing_rules(self) -> None:
        status, body = self.call("POST", "/api/settings/preview", {
            # 一个正确单号 + 一个位数对但前缀不对的（88 开头 15 位）
            "text": "621252607524819 881234567890123",
            "prefixes": "621",
            "lengths": "15",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["hits"], ["621252607524819"])
        values = [r["value"] for r in body["rejects"]]
        self.assertEqual(values, ["881234567890123"])
        self.assertTrue(any("前缀" in r["reason"] for r in body["rejects"]))

    def test_wechat_window_saved(self) -> None:
        status, body = self.call("POST", "/api/settings/wechat", {
            "group_name": "揽收群", "enabled": True,
            "window": "range", "window_start": "2026-10-01", "window_end": "2026-10-19",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"]["window"], "range")
        self.assertEqual(body["rules"]["window_start"], "2026-10-01")

    def test_wechat_range_requires_dates(self) -> None:
        status, _ = self.call("POST", "/api/settings/wechat", {
            "group_name": "揽收群", "window": "range", "window_start": "", "window_end": "",
        })
        self.assertEqual(status, 400)

    def test_wechat_interval_saved(self) -> None:
        """采集间隔：5 / 10 / 15 分钟由界面选，保存后写进规则与配置。"""
        status, body = self.call("POST", "/api/settings/wechat",
                                 {"group_name": "揽收群", "interval_sec": 600})
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"]["interval_sec"], 600)

        # 只改群名不该把间隔退回实时
        status, body = self.call("POST", "/api/settings/wechat", {"group_name": "另一个群"})
        self.assertEqual(body["rules"]["interval_sec"], 600)

        status, _ = self.call("POST", "/api/settings/wechat",
                              {"group_name": "揽收群", "interval_sec": 99999})
        self.assertEqual(status, 400)

    # ---------- 服务总开关 ----------

    def test_service_switch(self) -> None:
        """界面上唯一要点的按钮：开了就全自动，关了完全待机。"""
        status, body = self.call("POST", "/api/service", {"running": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["service"]["running"])
        # 统计里能读到状态，界面靠它渲染开关（不需要额外接口）
        status, stats = self.call("GET", "/api/stats")
        self.assertTrue(stats["service"]["running"])

        status, body = self.call("POST", "/api/service", {"running": False})
        self.assertEqual(status, 200)
        self.assertFalse(body["service"]["running"])
        status, stats = self.call("GET", "/api/stats")
        self.assertFalse(stats["service"]["running"])

    def test_service_switch_validates_input(self) -> None:
        status, _ = self.call("POST", "/api/service", {})
        self.assertEqual(status, 400)
        status, _ = self.call("POST", "/api/service", {"running": "yes"})
        self.assertEqual(status, 400)

    def test_service_switch_also_flips_collector(self) -> None:
        """总开关关掉时，采集必须同时停（不能「服务停了还在读微信」）。

        注意实现方式：总开关是**临时压制**，不改写用户设的采集开关，
        所以 enabled（用户意图）保持原样，而 active（此刻是否真采）为假。
        """
        self.call("POST", "/api/settings/wechat",
                  {"group_name": "测试群", "enabled": True})
        self.call("POST", "/api/service", {"running": True})
        status, body = self.call("GET", "/api/wechat/status")
        self.assertTrue(body["enabled"])
        self.assertTrue(body["active"])

        self.call("POST", "/api/service", {"running": False})
        status, body = self.call("GET", "/api/wechat/status")
        self.assertFalse(body["running"])
        self.assertFalse(body["active"], "服务停了不该还在读微信")
        self.assertTrue(body["enabled"], "用户设的采集开关被总开关吃掉了")

        # 开回服务就恢复采集，不用再手工开一次
        self.call("POST", "/api/service", {"running": True})
        status, body = self.call("GET", "/api/wechat/status")
        self.assertTrue(body["active"])

    # ---------- 手机取单 ----------

    def _clean_batch_state(self) -> None:
        """测试共享同一个库：把所有跑着/暂停的批次结束掉、清空队列。

        不能只看 stats 的 batch（它是「最近一个」）：同一秒创建的批次
        created_at 相同，排序不稳定，可能漏掉更早的 running 批次。
        """
        import sqlite3
        for _ in range(3):  # 防御：万一 abort 失败也别死循环
            with sqlite3.connect(self.config.db_path) as conn:
                rows = conn.execute(
                    "SELECT id FROM batches WHERE status IN ('running','paused')"
                ).fetchall()
            if not rows:
                break
            for (bid,) in rows:
                self.call("POST", f"/api/batches/{bid}/status", {"action": "abort"})
        with sqlite3.connect(self.config.db_path) as conn:
            conn.execute("DELETE FROM scan_jobs")

    def test_mobile_ticket_without_batch(self) -> None:
        self._clean_batch_state()
        status, body = self.call("POST", "/api/mobile/ticket", {})
        self.assertEqual(status, 400)
        self.assertIn("批次", body["error"]["message"])

    def test_mobile_ticket_and_text(self) -> None:
        self._clean_batch_state()
        svc = self.httpd.app.service
        svc.enqueue(["770000000000001", "770000000000002", "770000000000003"], source="test")
        svc.start_batch(3, actor="test")

        status, body = self.call("POST", "/api/mobile/ticket", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 3)
        self.assertIn("/m/", body["url"])

        ticket = body["ticket"]
        # 手机浏览器带不了 X-Token，/m/ 靠票据自证，必须匿名可取
        with urllib.request.urlopen(self.base + "/m/" + ticket, timeout=5) as page:
            self.assertEqual(page.status, 200)
            self.assertEqual(page.headers["Content-Type"], "text/html; charset=utf-8")

        status, data = self.call("GET", "/api/mobile/text?t=" + ticket)
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["text"], "770000000000001,770000000000002,770000000000003")

        # 篡改签名 / 过期时间，都要 401
        status, err = self.call("GET", "/api/mobile/text?t=" + ticket[:-2] + "zz")
        self.assertEqual(status, 401)
        stale = ticket.split(".")
        stale[1] = "1000000000"
        status, err = self.call("GET", "/api/mobile/text?t=" + ".".join(stale))
        self.assertEqual(status, 401)
        self.assertIn("过期", err["error"]["message"])


    def test_phone_next_and_report(self) -> None:
        self._clean_batch_state()
        svc = self.httpd.app.service
        nums = ["770000000000011", "770000000000012", "770000000000013"]
        svc.enqueue(nums, source="test")
        batch = svc.start_batch(3, actor="test")
        k = self.config.server.token

        # key 不对直接 401（手机脚本没有 X-Token，只能靠 URL 里的 k）
        status, _ = self.call("GET", "/api/phone/next?k=wrong")
        self.assertEqual(status, 401)

        status, data = self.call("GET", f"/api/phone/next?k={k}")
        self.assertEqual(status, 200)
        self.assertEqual(data["batch_id"], batch["id"])
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["text"], ",".join(nums))

        status, dump = self.call("POST", f"/api/phone/dump?k={k}", {"tag": "scan", "dump": "<x/>"})
        self.assertEqual(status, 200)
        self.assertTrue((self.config.db_path.parent / "phone_dumps" / dump["file"]).exists())

        # 回传结果：1 条失败，其余落 ok，批次随之收口
        status, res = self.call(
            "POST",
            f"/api/phone/report?k={k}",
            {"batch_id": batch["id"], "fail": [nums[1]], "finished": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["marked"], {"ok": 2, "fail": 1})
        import sqlite3

        with sqlite3.connect(self.config.db_path) as conn:
            # 失败单号按规则回 pending 等重试（max_attempts 未到），不会被静默判死
            left = conn.execute(
                "SELECT waybill FROM scan_jobs WHERE batch_id = ? AND status = 'pending'",
                (batch["id"],),
            ).fetchall()
        self.assertEqual([r[0] for r in left], [nums[1]])

        # 重复回传不会再改状态（已经没有 pending 了）
        status, res = self.call(
            "POST",
            f"/api/phone/report?k={k}",
            {"batch_id": batch["id"], "finished": True},
        )
        self.assertEqual(res["marked"], {"ok": 0, "fail": 0})

    def test_phone_report_explicit_list_without_hand(self) -> None:
        """adb 脚本显式报「这一轮提交了哪几条」：服务端重启丢了内存也不影响结算。"""
        self._clean_batch_state()
        svc = self.httpd.app.service
        nums = ["770000000000021", "770000000000022", "770000000000023"]
        svc.enqueue(nums, source="test")
        batch = svc.start_batch(3, actor="test")
        k = self.config.server.token

        self.call("GET", f"/api/phone/next?k={k}")
        self.httpd.app._phone_hand.clear()  # 模拟 miao-core 重启，内存里的交付记录没了

        status, res = self.call(
            "POST",
            f"/api/phone/report?k={k}",
            {
                "batch_id": batch["id"],
                "finished": True,
                "ok": [nums[0], nums[1]],
                "fail": [{"waybill": nums[2], "reason": "超重"}],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["marked"], {"ok": 2, "fail": 1})

        import sqlite3

        with sqlite3.connect(self.config.db_path) as conn:
            rows = dict(
                conn.execute(
                    "SELECT waybill, status FROM scan_jobs WHERE batch_id = ?", (batch["id"],)
                ).fetchall()
            )
        self.assertEqual(rows[nums[0]], "ok")
        self.assertEqual(rows[nums[1]], "ok")
        # 第一次失败按规则回 pending 重试，并留下原因
        self.assertEqual(rows[nums[2]], "pending")

        # 不在本批的单号不会误判，原样回给调用方
        status, res = self.call(
            "POST",
            f"/api/phone/report?k={k}",
            {"batch_id": batch["id"], "finished": True, "ok": ["770000000000999"]},
        )
        self.assertEqual(res["marked"], {"ok": 0, "fail": 0})
        self.assertEqual(res["unmatched"], ["770000000000999"])

    def test_phone_report_multiple_intercepts_are_final(self) -> None:
        """一批里多个拦截件：都落终态、原因写「拦截」、批次正常收口。

        拦截件是申行者弹窗「移除并继续」摘出去的，注定揽不了 —— 脚本回传
        final=true。若按普通失败走 pending，下一轮又被注入又被移除，
        死循环卡住批次收口（实测 2026-10-26 卡死过一条）。
        """
        self._clean_batch_state()
        svc = self.httpd.app.service
        nums = ["770000000000031", "770000000000032", "770000000000033", "770000000000034"]
        svc.enqueue(nums, source="test")
        batch = svc.start_batch(4, actor="test")
        k = self.config.server.token
        self.call("GET", f"/api/phone/next?k={k}")

        status, res = self.call(
            "POST",
            f"/api/phone/report?k={k}",
            {
                "batch_id": batch["id"],
                "finished": True,
                "ok": [nums[0], nums[3]],
                "fail": [
                    {"waybill": nums[1], "reason": "拦截", "final": True},
                    {"waybill": nums[2], "reason": "拦截", "final": True},
                ],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(res["marked"], {"ok": 2, "fail": 2})

        import sqlite3

        with sqlite3.connect(self.config.db_path) as conn:
            rows = dict(
                conn.execute(
                    "SELECT waybill, status FROM scan_jobs WHERE batch_id = ?", (batch["id"],)
                ).fetchall()
            )
            errors = dict(
                conn.execute(
                    "SELECT waybill, last_error FROM scan_jobs WHERE batch_id = ?", (batch["id"],)
                ).fetchall()
            )
            bstatus = conn.execute(
                "SELECT status FROM batches WHERE id = ?", (batch["id"],)
            ).fetchone()[0]
        self.assertEqual(rows[nums[0]], "ok")
        self.assertEqual(rows[nums[3]], "ok")
        self.assertEqual(rows[nums[1]], "fail")
        self.assertEqual(rows[nums[2]], "fail")
        self.assertEqual(errors[nums[1]], "拦截")
        self.assertEqual(errors[nums[2]], "拦截")
        # 全部终态 → 批次自动收口
        self.assertEqual(bstatus, "done")


if __name__ == "__main__":
    unittest.main()
