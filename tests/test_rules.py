from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scan.db import migrate  # noqa: E402
from scan.errors import ValidationError  # noqa: E402
from scan.rules import RuleStore, normalize_lengths, normalize_prefixes  # noqa: E402
from tests.test_core import make_config  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_prefix_forms(self) -> None:
        self.assertEqual(normalize_prefixes("77, 621"), ("77", "621"))
        self.assertEqual(normalize_prefixes("77，621"), ("77", "621"))  # 全角逗号
        self.assertEqual(normalize_prefixes(["77", " 621 "]), ("77", "621"))
        self.assertEqual(normalize_prefixes("77,77,621"), ("77", "621"))  # 去重保序
        self.assertEqual(normalize_prefixes("6"), ("6",))  # 「6 开头」这种单数字
        self.assertEqual(normalize_prefixes(""), ())

    def test_prefix_invalid(self) -> None:
        for bad in ("abc", "12345", "77a", "-7", "7.7"):
            with self.assertRaises(ValidationError, msg=bad):
                normalize_prefixes(bad)

    def test_length_forms(self) -> None:
        self.assertEqual(normalize_lengths("15"), (15,))
        self.assertEqual(normalize_lengths("12,13,15"), (12, 13, 15))
        self.assertEqual(normalize_lengths("15,12,12"), (12, 15))

    def test_length_invalid(self) -> None:
        for bad in ("0", "31", "abc", "-1", "1.5"):
            with self.assertRaises(ValidationError, msg=bad):
                normalize_lengths(bad)


class RuleStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        migrate(self.config.db_path)
        self.cfg_file = self.tmp / "config.yaml"

    def _store(self) -> RuleStore:
        return RuleStore(self.config, config_path=self.cfg_file)

    def test_defaults_from_config(self) -> None:
        store = self._store()
        self.assertEqual(store.prefixes, self.config.scan.prefixes)
        self.assertEqual(store.lengths, self.config.scan.lengths)
        self.assertEqual(store.snapshot()["source"], "config")

    def test_update_writes_file_and_survives_restart(self) -> None:
        store = self._store()
        snap = store.update("77, 621", "15")
        self.assertEqual(snap["prefixes"], ["77", "621"])
        self.assertEqual(snap["lengths"], [15])

        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["scan"]["prefixes"], ["77", "621"])
        self.assertEqual(data["scan"]["lengths"], [15])

        # 新实例 = 模拟重启：从 runtime 读回新规则
        again = self._store()
        self.assertEqual(again.prefixes, ("77", "621"))
        self.assertEqual(again.lengths, (15,))
        self.assertEqual(again.snapshot()["source"], "runtime")

    def test_update_keeps_other_config_keys(self) -> None:
        self.cfg_file.write_text(
            yaml.safe_dump(
                {"scan": {"prefixes": ["77"], "lengths": [15], "batch_max": 7},
                 "server": {"port": 9999}},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        self._store().update("621", "12")
        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["scan"]["batch_max"], 7)   # 别人的配置不能被抹掉
        self.assertEqual(data["server"]["port"], 9999)
        self.assertEqual(data["scan"]["prefixes"], ["621"])

    def test_both_empty_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._store().update([], [])

    def test_live_reload_from_db(self) -> None:
        """两个 RuleStore 指向同一个库：A 改完，B 重新加载能看到。"""
        a = self._store()
        b = self._store()
        a.update("621", "15")
        self.assertEqual(b.prefixes, self.config.scan.prefixes)  # B 还没刷新
        b._reload()  # noqa: SLF001 - 验证机制本身
        self.assertEqual(b.prefixes, ("621",))
        self.assertEqual(b.lengths, (15,))


class GroupSettingTest(unittest.TestCase):
    """采集群名：界面上可自定义，改完立刻生效、重启仍有效。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        migrate(self.config.db_path)
        self.cfg_file = self.tmp / "config.yaml"

    def _store(self) -> RuleStore:
        return RuleStore(self.config, config_path=self.cfg_file)

    def test_update_group(self) -> None:
        store = self._store()
        snap = store.update_group("  自动揽收测试群，支持明文单号excel  ")
        self.assertEqual(snap["group_name"], "自动揽收测试群，支持明文单号excel")
        self.assertEqual(store.group_name, "自动揽收测试群，支持明文单号excel")
        # 写回文件：重启后仍是这个群
        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["wechat"]["group_name"], "自动揽收测试群，支持明文单号excel")

    def test_group_survives_reload(self) -> None:
        self._store().update_group("另一个揽收群")
        self.assertEqual(self._store().group_name, "另一个揽收群")

    def test_clear_group_means_pause(self) -> None:
        store = self._store()
        store.update_group("某群")
        store.update_group("")
        self.assertEqual(store.group_name, "")

    def test_group_too_long_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._store().update_group("群" * 101)

    def test_enabled_toggle(self) -> None:
        store = self._store()
        self.assertEqual(store.enabled, self.config.wechat.enabled)  # 默认来自 config
        store.update_group("某群", enabled=True)
        self.assertTrue(store.enabled)
        store.update_group("某群", enabled=False)
        self.assertFalse(store.enabled)
        self.assertFalse(self._store().enabled)  # 重启后仍是关闭
        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertFalse(data["wechat"]["enabled"])

    def test_group_change_keeps_scan_rules(self) -> None:
        store = self._store()
        store.update("621", "15")
        store.update_group("某群")
        self.assertEqual(store.prefixes, ("621",))
        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["scan"]["prefixes"], ["621"])

    # ---------- 采集间隔：别一直抢鼠标 ----------

    def test_interval_default_from_config(self) -> None:
        store = self._store()
        self.assertEqual(store.snapshot()["interval_sec"], self.config.wechat.interval_sec)

    def test_interval_saved_and_survives_reload(self) -> None:
        store = self._store()
        snap = store.update_group("某群", interval_sec=600)
        self.assertEqual(snap["interval_sec"], 600)
        self.assertEqual(store.interval_sec, 600)
        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["wechat"]["interval_sec"], 600)
        # 新实例 = 模拟重启
        self.assertEqual(self._store().interval_sec, 600)

    def test_interval_kept_when_only_group_changes(self) -> None:
        store = self._store()
        store.update_group("某群", interval_sec=900)
        store.update_group("另一个群")
        self.assertEqual(store.interval_sec, 900)
        self.assertEqual(store.group_name, "另一个群")

    def test_interval_back_to_realtime(self) -> None:
        store = self._store()
        store.update_group("某群", interval_sec=300)
        store.update_group("某群", interval_sec=0)
        self.assertEqual(store.interval_sec, 0)

    def test_interval_validation(self) -> None:
        for bad in (-1, 7201, "abc", ""):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                self._store().update_group("某群", interval_sec=bad)


class LimitsAndWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        migrate(self.config.db_path)
        self.cfg_file = self.tmp / "config.yaml"
        self.store = RuleStore(self.config, config_path=self.cfg_file)

    def test_limits_persist(self) -> None:
        self.store.update_limits(30, 3)
        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["scan"]["batch_max"], 30)
        self.assertEqual(data["scan"]["max_attempts"], 3)
        again = RuleStore(self.config, config_path=self.cfg_file)
        self.assertEqual((again.batch_max, again.max_attempts), (30, 3))

    def test_limits_out_of_range(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.update_limits(0, 2)
        with self.assertRaises(ValidationError):
            self.store.update_limits(10, 11)

    def test_window_saved_and_validated(self) -> None:
        self.store.update_group("某群", window="range",
                                window_start="2026-10-01", window_end="2026-10-19")
        self.assertEqual(self.store.window, ("range", "2026-10-01", "2026-10-19"))
        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(data["wechat"]["window"], "range")

        with self.assertRaises(ValidationError):
            self.store.update_group("某群", window="range", window_start="", window_end="")
        with self.assertRaises(ValidationError):
            self.store.update_group("某群", window="range",
                                    window_start="2026-10-19", window_end="2026-10-01")
        with self.assertRaises(ValidationError):
            self.store.update_group("某群", window_start="10/19")
        with self.assertRaises(ValidationError):
            self.store.update_group("某群", window="yesterday")


class ServiceSwitchTest(unittest.TestCase):
    """服务总开关：一键开启 = 全自动；关掉 = 完全待机。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        migrate(self.config.db_path)
        self.cfg_file = self.tmp / "config.yaml"
        self.store = RuleStore(self.config, config_path=self.cfg_file)

    def test_default_on(self) -> None:
        """无人值守程序：装机/重启后默认就该在跑，不该要求人先点一次开启。"""
        self.assertTrue(self.store.running)
        self.assertTrue(self.store.snapshot()["running"])

    def test_toggle_and_persist(self) -> None:
        # 先把采集开关打开（make_config 里 wechat.enabled 默认为 False）
        self.store.update_group(self.store.snapshot()["group_name"] or "群", enabled=True)

        self.store.set_running(False)
        self.assertFalse(self.store.running)
        # 总开关关闭只是「临时压制」，采集开关本身不该被改写
        self.assertFalse(self.store.enabled)

        data = yaml.safe_load(self.cfg_file.read_text(encoding="utf-8"))
        self.assertFalse(data["scan"]["service_running"])

        # 重启进程后仍然是「关着」——人主动关的要尊重
        again = RuleStore(self.config, config_path=self.cfg_file)
        self.assertFalse(again.running)
        self.assertFalse(again.enabled)

        self.store.set_running(True)
        again = RuleStore(self.config, config_path=self.cfg_file)
        self.assertTrue(again.running)
        self.assertTrue(again.enabled)

    def test_collect_switch_survives_service_cycle(self) -> None:
        """停服务再开回来，用户设的「启用采集」不能被吃掉。

        旧实现 set_running(False) 会把 _enabled 一起写成 false，
        开回服务后采集仍是关的 —— 表现成「重启后采集没反应」。
        """
        self.store.update_group(self.store.snapshot()["group_name"] or "群", enabled=True)
        self.store.set_running(False)
        self.store.set_running(True)
        self.assertTrue(self.store.snapshot()["enabled"], "采集开关被总开关吃掉了")
        self.assertTrue(self.store.enabled)

    def test_must_be_bool(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.set_running("yes")  # type: ignore[arg-type]

    def test_dispatch_gated_by_switch(self) -> None:
        from scan.service import JobService

        svc = JobService(self.config.db_path, self.config, self.store)
        svc.enqueue(["7712345678901"], source="wx")
        self.store.set_running(False)
        self.assertIsNone(svc.auto_dispatch())     # 关着不发（哪怕采集开关是开的）
        self.store.set_running(True)
        batch = svc.auto_dispatch()                # 开了立刻发
        self.assertIsNotNone(batch)
        self.assertEqual(batch["limit_n"], 1)


if __name__ == "__main__":
    unittest.main()
