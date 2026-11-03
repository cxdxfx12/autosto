from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scan.config import WeChatConfig  # noqa: E402
from scan.db import migrate  # noqa: E402
from scan.extract.wechat import (  # noqa: E402
    BUBBLE_ITEM,
    TEXT_ITEM,
    TIME_ITEM,
    FileRef,
    WeChatCollector,
    WeChatFileLocator,
    WeChatUnavailable,
    base_stem,
    group_matches,
    is_time_separator,
    parse_file_bubble,
    parse_items,
    parse_separator,
)
from scan.service import JobService  # noqa: E402
from tests.test_core import make_config  # noqa: E402


class FakeReader:
    def __init__(self, title: str, items: list[tuple[str, str]]) -> None:
        self._title = title
        self._items = items

    def open(self) -> None:
        pass

    def title(self) -> str:
        return self._title

    def read_items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def close(self) -> None:
        pass


class ParseTest(unittest.TestCase):
    def test_time_separators(self) -> None:
        for text in ("12:25", "昨天 19:12", "星期一", "10-16", "2026年10月16日 12:25"):
            self.assertTrue(is_time_separator(text), text)
        self.assertFalse(is_time_separator("7712345678901"))

    def test_parse_filters_and_classifies(self) -> None:
        items = [
            (TIME_ITEM, "12:25"),
            (TEXT_ITEM, "@。zz 麻烦揽收下 7712345678901 谢谢"),
            (TIME_ITEM, "昨天 19:12"),
            (BUBBLE_ITEM, "文件\n10.16上午需揽收.xlsx\n35.7K\n微信电脑版"),
            ("mmui::XTextView", "发送"),
        ]
        messages = parse_items(items)
        self.assertEqual([m.kind for m in messages], ["text", "file"])
        self.assertIn("7712345678901", messages[0].text)
        self.assertTrue(all(m.fingerprint for m in messages))
        # 同一条消息重复出现 → 指纹相同，用于第二道去重
        again = parse_items([(TEXT_ITEM, "@。zz 麻烦揽收下 7712345678901 谢谢")])
        self.assertEqual(again[0].fingerprint, messages[0].fingerprint)

    def test_group_name_with_member_count(self) -> None:
        self.assertTrue(group_matches("香飘飘申通揽收(13)", "香飘飘申通揽收"))
        self.assertTrue(group_matches("香飘飘申通揽收（13）", "香飘飘申通揽收"))
        self.assertFalse(group_matches("别的群", "香飘飘申通揽收"))


class CollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = replace(
            make_config(self.tmp),
            wechat=WeChatConfig(enabled=True, group_name="揽收专用群", poll_ms=1000, max_messages=50),
        )
        migrate(self.config.db_path)
        self.svc = JobService(self.config.db_path, self.config)

    def test_baseline_then_only_new(self) -> None:
        items = [
            (TIME_ITEM, "12:25"),
            (TEXT_ITEM, "历史消息 7712345678901"),
        ]
        reader = FakeReader("揽收专用群(13)", items)
        collector = WeChatCollector(self.config, self.svc, reader=reader)

        # 第一次：只建基线，不采历史
        first = collector.poll_once()
        self.assertEqual(first["baselined"], 1)
        self.assertEqual(first["inserted"], 0)
        self.assertEqual(self.svc.list_jobs(), [])

        # 群里来了一条新的
        items.append((TIME_ITEM, "12:31"))
        items.append((TEXT_ITEM, "这两票麻烦扫一下 7712345678902 7712345678903"))
        second = collector.poll_once()
        self.assertEqual(second["inserted"], 2)
        waybills = {j["waybill"] for j in self.svc.list_jobs()}
        self.assertEqual(waybills, {"7712345678902", "7712345678903"})

        # 再轮询一次：没有新消息，不重复入库
        third = collector.poll_once()
        self.assertEqual(third["new"], 0)
        self.assertEqual(len(self.svc.list_jobs()), 2)

    def test_database_dedupe_across_sources(self) -> None:
        # 已经在 Excel 里入过的号，微信又发一遍 → 不重复入队
        self.svc.enqueue(["7712345678901"], source="excel")
        reader = FakeReader("揽收专用群", [])
        collector = WeChatCollector(self.config, self.svc, reader=reader)
        collector.poll_once()  # 空窗口建基线

        reader._items = [(TEXT_ITEM, "7712345678901")]
        result = collector.poll_once()
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(len(self.svc.list_jobs()), 1)

    def test_wrong_group_refuses(self) -> None:
        reader = FakeReader("无关的群", [(TEXT_ITEM, "7712345678901")])
        collector = WeChatCollector(self.config, self.svc, reader=reader)
        with self.assertRaises(WeChatUnavailable):
            collector.poll_once()
        self.assertEqual(self.svc.list_jobs(), [])


class FileBubbleTest(unittest.TestCase):
    def test_parse_normal_bubble(self) -> None:
        ref = parse_file_bubble("文件\n10.19上午需揽收.xlsx\n40.0K\n微信电脑版")
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref.name, "10.19上午需揽收.xlsx")
        self.assertAlmostEqual(ref.kb or 0, 40.0, places=2)

    def test_parse_without_size(self) -> None:
        ref = parse_file_bubble("文件\n丹鸟.xlsx\n微信电脑版")
        assert ref is not None
        self.assertEqual(ref.name, "丹鸟.xlsx")
        self.assertIsNone(ref.kb)

    def test_parse_megabyte(self) -> None:
        ref = parse_file_bubble("文件\n大表.xlsx\n1.5M\n微信电脑版")
        assert ref is not None
        self.assertAlmostEqual(ref.kb or 0, 1536.0, places=1)

    def test_not_a_table(self) -> None:
        self.assertIsNone(parse_file_bubble("文件\n通知.pdf\n12.0K\n微信电脑版"))
        self.assertIsNone(parse_file_bubble("7712345678901"))
        self.assertIsNone(parse_file_bubble("文件"))

    def test_base_stem(self) -> None:
        self.assertEqual(base_stem("丹鸟(17)"), "丹鸟")
        self.assertEqual(base_stem("10.19上午需揽收(1)"), "10.19上午需揽收")
        self.assertEqual(base_stem("丹鸟下午"), "丹鸟下午")
        self.assertEqual(base_stem("丹鸟"), "丹鸟")


def _make_xlsx(path: Path, waybills: list[object], sheet: str = "Sheet1") -> Path:
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(["序号", "运单号"])
    for i, value in enumerate(waybills, start=1):
        ws.append([i, value])
    wb.save(str(path))
    return path


class FileLocatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.month = self.root / "wxid_abc" / "msg" / "file" / "2026-10"
        self.month.mkdir(parents=True)

    def test_pick_by_size_not_by_name(self) -> None:
        # 同一天两份丹鸟：旧的是另一份内容，大小不同
        old = self.month / "丹鸟.xlsx"
        old.write_bytes(b"x" * 5000)
        new = self.month / "丹鸟(17).xlsx"
        new.write_bytes(b"y" * 11051)

        loc = WeChatFileLocator(self.root)
        found = loc.find(FileRef("丹鸟.xlsx", 11051 / 1024))
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.name, "丹鸟(17).xlsx")

    def test_size_mismatch_returns_none(self) -> None:
        (self.month / "丹鸟.xlsx").write_bytes(b"x" * 5000)
        loc = WeChatFileLocator(self.root)
        self.assertIsNone(loc.find(FileRef("丹鸟.xlsx", 999.0)))

    def test_other_names_not_confused(self) -> None:
        (self.month / "丹鸟下午(15).xlsx").write_bytes(b"x" * 11051)
        loc = WeChatFileLocator(self.root)
        self.assertIsNone(loc.find(FileRef("丹鸟.xlsx", 11051 / 1024)))

    def test_missing_root(self) -> None:
        loc = WeChatFileLocator(self.root / "not-exists")
        self.assertIsNone(loc.find(FileRef("丹鸟.xlsx", 1.0)))


class FileIngestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "xwechat_files"
        self.month = self.root / "wxid_abc" / "msg" / "file" / "2026-10"
        self.month.mkdir(parents=True)
        self.config = replace(
            make_config(self.tmp),
            wechat=WeChatConfig(
                enabled=True,
                group_name="自动揽收测试群",
                poll_ms=1000,
                max_messages=50,
                file_root=self.root,
            ),
        )
        migrate(self.config.db_path)
        self.svc = JobService(self.config.db_path, self.config)

    def _bubble(self, path: Path, shown: str) -> tuple[str, str]:
        kb = path.stat().st_size / 1024
        return (BUBBLE_ITEM, f"文件\n{shown}\n{kb:.1f}K\n微信电脑版")

    def test_ingest_files_on_first_run(self) -> None:
        """首轮就要采到文件——这正是「文件不走基线」的意义。"""
        f1 = _make_xlsx(self.month / "10.19上午需揽收.xlsx", [7712345678901, "7712345678902"])
        f2 = _make_xlsx(self.month / "丹鸟(17).xlsx", ["7723456789012"])
        items = [
            (TIME_ITEM, "15:25"),
            self._bubble(f1, "10.19上午需揽收.xlsx"),
            self._bubble(f2, "丹鸟.xlsx"),
        ]
        collector = WeChatCollector(
            self.config, self.svc, reader=FakeReader("自动揽收测试群(3)", items)
        )
        first = collector.poll_once()
        self.assertEqual(first["baselined"], 1)  # 文字基线仍建立
        self.assertEqual(first["files"], 2)      # 但两份表都入库了
        self.assertEqual(first["inserted"], 3)
        waybills = {j["waybill"] for j in self.svc.list_jobs()}
        self.assertEqual(waybills, {"7712345678901", "7712345678902", "7723456789012"})

    def test_same_bubble_not_imported_twice(self) -> None:
        """微信会给同一个文件存两份一模一样的副本（10.19...xlsx 与 (1).xlsx）。

        群消息里两气泡同名同大小，在消息级就被判为同一份，只处理一次。
        """
        a = _make_xlsx(self.month / "10.19上午需揽收.xlsx", [7712345678901])
        b = self.month / "10.19上午需揽收(1).xlsx"
        b.write_bytes(a.read_bytes())
        items = [
            (TIME_ITEM, "15:25"),
            self._bubble(a, "10.19上午需揽收.xlsx"),
            self._bubble(b, "10.19上午需揽收.xlsx"),
        ]
        collector = WeChatCollector(
            self.config, self.svc, reader=FakeReader("自动揽收测试群", items)
        )
        result = collector.poll_once()
        self.assertEqual(result["files"], 1)
        self.assertEqual(len(self.svc.list_jobs()), 1)

        # 新进程再跑一遍（模拟重启）：imports 表按内容哈希挡住，仍然不重复
        again = WeChatCollector(
            self.config, self.svc, reader=FakeReader("自动揽收测试群", items)
        )
        second = again.poll_once()
        self.assertEqual(second["files"], 0)
        self.assertEqual(len(self.svc.list_jobs()), 1)

    def test_same_content_different_name_skipped(self) -> None:
        """两个不同名的表内容一模一样 → 内容哈希命中，第二份不再入队。"""
        a = _make_xlsx(self.month / "丹鸟.xlsx", [7712345678901])
        b = self.month / "丹鸟下午.xlsx"
        b.write_bytes(a.read_bytes())
        items = [
            (TIME_ITEM, "15:25"),
            self._bubble(a, "丹鸟.xlsx"),
            self._bubble(b, "丹鸟下午.xlsx"),
        ]
        collector = WeChatCollector(
            self.config, self.svc, reader=FakeReader("自动揽收测试群", items)
        )
        result = collector.poll_once()
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(self.svc.list_jobs()), 1)

    def test_missing_file_reported(self) -> None:
        items = [
            (TIME_ITEM, "15:25"),
            (BUBBLE_ITEM, "文件\n没有下载完的.xlsx\n40.0K\n微信电脑版"),
        ]
        collector = WeChatCollector(
            self.config, self.svc, reader=FakeReader("自动揽收测试群", items)
        )
        result = collector.poll_once()
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["files"], 0)
        self.assertEqual(self.svc.list_jobs(), [])


class TimeWindowTest(unittest.TestCase):
    """只采当天 / 指定区间的过滤。核心风险：把昨天的单再扫一遍 = 多出真实轨迹。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _config(self, window: str = "today", start: str = "", end: str = ""):
        return replace(
            make_config(self.tmp),
            wechat=WeChatConfig(
                enabled=True,
                group_name="揽收专用群",
                poll_ms=1000,
                max_messages=50,
                window=window,
                window_start=start,
                window_end=end,
            ),
        )

    def _collect(self, config, first, second):
        migrate(config.db_path)
        svc = JobService(config.db_path, config)
        c1 = WeChatCollector(config, svc, reader=FakeReader("揽收专用群(13)", first))
        c1.poll_once()  # 第一轮只建基线
        c2 = WeChatCollector(config, svc, reader=FakeReader("揽收专用群(13)", second))
        return c2.poll_once(), svc

    def test_today_skips_yesterday(self) -> None:
        cfg = self._config("today")
        first = [(TIME_ITEM, "09:00"), (TEXT_ITEM, "基线")]
        second = first + [
            (TIME_ITEM, "昨天 19:12"),
            (TEXT_ITEM, "昨天的单 7712345678901"),
            (TIME_ITEM, "10:00"),
            (TEXT_ITEM, "今天的单 7712345678902"),
        ]
        result, svc = self._collect(cfg, first, second)
        self.assertEqual([j["waybill"] for j in svc.list_jobs()], ["7712345678902"])
        self.assertEqual(result["out_of_window"], 1)

    def test_all_mode_keeps_yesterday(self) -> None:
        cfg = self._config("all")
        first = [(TIME_ITEM, "09:00"), (TEXT_ITEM, "基线")]
        second = first + [
            (TIME_ITEM, "昨天 19:12"),
            (TEXT_ITEM, "昨天的单 7712345678901"),
        ]
        _result, svc = self._collect(cfg, first, second)
        self.assertEqual([j["waybill"] for j in svc.list_jobs()], ["7712345678901"])

    def test_no_separator_assumes_today(self) -> None:
        """整屏没有时间分隔条（短会话常见）：不该把消息全丢掉。"""
        cfg = self._config("today")
        first = [(TEXT_ITEM, "基线")]
        second = first + [(TEXT_ITEM, "7712345678903")]
        _result, svc = self._collect(cfg, first, second)
        self.assertEqual([j["waybill"] for j in svc.list_jobs()], ["7712345678903"])

    def test_range_includes_that_day(self) -> None:
        from datetime import date, timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        cfg = self._config("range", yesterday, today)
        first = [(TIME_ITEM, "09:00"), (TEXT_ITEM, "基线")]
        second = first + [
            (TIME_ITEM, "昨天 19:12"),
            (TEXT_ITEM, "7712345678904"),
            (TIME_ITEM, "前天 19:12"),
            (TEXT_ITEM, "7712345678905"),
        ]
        _result, svc = self._collect(cfg, first, second)
        self.assertEqual([j["waybill"] for j in svc.list_jobs()], ["7712345678904"])

    def test_parse_separator_forms(self) -> None:
        from datetime import datetime

        now = datetime(2026, 10, 19, 16, 30)
        self.assertEqual(parse_separator("09:35", now).date().isoformat(), "2026-10-19")
        self.assertEqual(parse_separator("昨天 19:12", now).date().isoformat(), "2026-10-18")
        self.assertEqual(parse_separator("前天 08:00", now).date().isoformat(), "2026-10-17")
        self.assertEqual(parse_separator("星期二", now).date().isoformat(), "2026-10-13")
        self.assertEqual(parse_separator("10月16日", now).date().isoformat(), "2026-10-16")
        self.assertIsNone(parse_separator("随便一句", now))


class ShellHealTest(unittest.TestCase):
    """自绘壳自愈：必须「只重启一次 + 冷却」和「文案能指导人」，绝不死循环。"""

    def setUp(self) -> None:
        from scan.extract import wechat as wx

        self.wx = wx
        self._orig_at = wx._SHELL_HEAL_AT
        self._orig_restart = wx._restart_weixin
        wx._SHELL_HEAL_AT = 0.0
        self.calls: list[int] = []
        wx._restart_weixin = lambda: (self.calls.append(1), "已重启")[1]

    def tearDown(self) -> None:
        self.wx._SHELL_HEAL_AT = self._orig_at
        self.wx._restart_weixin = self._orig_restart

    def test_first_detection_restarts(self) -> None:
        msg = self.wx._shell_message("Qt51514QWindowIcon")
        self.assertEqual(msg, "已重启")
        self.assertEqual(len(self.calls), 1)

    def test_second_detection_does_not_restart(self) -> None:
        """冷却期内再报错不能再杀微信——否则微信反复退化时会一直重启。"""
        self.wx._shell_message("Qt51514QWindowIcon")
        self.wx._shell_message("Qt51514QWindowIcon")
        self.wx._shell_message("Qt51514QWindowIcon")
        self.assertEqual(len(self.calls), 1, "冷却期内只允许重启一次")

    def test_cooldown_message_is_actionable(self) -> None:
        self.wx._shell_message("Qt51514QWindowIcon")
        msg = self.wx._shell_message("Qt51514QWindowIcon")
        self.assertIn("自绘壳", msg)
        self.assertIn("进入微信", msg)
        self.assertIn("不再重复重启", msg)

    def test_cooldown_expires(self) -> None:
        self.wx._shell_message("Qt51514QWindowIcon")
        # 把上次自愈时间往回拨到冷却窗口之外
        self.wx._SHELL_HEAL_AT -= self.wx._SHELL_HEAL_COOLDOWN + 1
        self.wx._shell_message("Qt51514QWindowIcon")
        self.assertEqual(len(self.calls), 2, "冷却结束后应允许再重启一次")

    def test_restart_message_without_exe_is_honest(self) -> None:
        """找不到微信路径时必须让人手动开，不能假装成功。"""
        import subprocess

        wx = self.wx
        orig_exe, orig_pids, orig_run = wx._weixin_exe, wx._weixin_pids, subprocess.run
        wx._restart_weixin = self._orig_restart   # 用真实实现，不走 setUp 的桩
        try:
            wx._weixin_pids = lambda: [1234]
            wx._weixin_exe = lambda: None
            wx.time.sleep = lambda *_: None
            subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})()
            msg = wx._restart_weixin()
        finally:
            wx._weixin_exe, wx._weixin_pids = orig_exe, orig_pids
            subprocess.run = orig_run
        self.assertIn("手动", msg)

    def test_no_weixin_process_is_honest(self) -> None:
        """微信压根没在跑时，别去 taskkill 空气，直接让人手动开。"""
        wx = self.wx
        orig_pids = wx._weixin_pids
        wx._restart_weixin = self._orig_restart
        try:
            wx._weixin_pids = lambda: []
            msg = wx._restart_weixin()
        finally:
            wx._weixin_pids = orig_pids
        self.assertIn("手动", msg)
        self.assertIn("微信进程不在", msg)

    def test_constants_shared_with_reader(self) -> None:
        """类内 WINDOW_CLASS 必须跟模块常量一致，防止两处漂移成不同类名。"""
        self.assertEqual(self.wx.WeChatReader.WINDOW_CLASS, self.wx.MAIN_WINDOW_CLASS)


class FixedStateTest(unittest.TestCase):
    """采集被切到「关闭」后，报错文案要能指导人恢复。"""

    def test_disabled_message_points_to_switch(self) -> None:
        # ignore_cleanup_errors：JobService 不关连接，Windows 删不掉临时库（与断言无关）
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            cfg = make_config(Path(tmp))
            migrate(cfg.db_path)
            svc = JobService(cfg.db_path, cfg)
            reader = FakeReader("群", [(TIME_ITEM, "09:00"), (TEXT_ITEM, "基线")])
            col = WeChatCollector(cfg, svc, reader=reader)
            # enabled 是只读属性（读 rules / config），用「关掉的子配置」造这个状态
            col.config = replace(cfg, wechat=replace(cfg.wechat, enabled=False))
            with self.assertRaises(WeChatUnavailable) as ctx:
                col.poll_once()
        self.assertIn("关闭", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class BaseStemTest(unittest.TestCase):
    """微信重名副本 + 文件名自带 (N) 的双层后缀（2026-10-29 丢 98 单的根因）。"""

    def test_single_copy_suffix(self):
        from scan.extract.wechat import base_stem
        self.assertEqual(base_stem("丹鸟(17)"), "丹鸟")

    def test_double_copy_suffix_meets_message_name(self):
        from scan.extract.wechat import base_stem
        on_disk = base_stem("新建 XLSX 工作表 (2)(1)")
        in_chat = base_stem("新建 XLSX 工作表 (2)")
        self.assertEqual(on_disk, in_chat)
        self.assertEqual(on_disk, "新建 XLSX 工作表")

    def test_plain_name_untouched(self):
        from scan.extract.wechat import base_stem
        self.assertEqual(base_stem("10.29下午需揽收"), "10.29下午需揽收")
