import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scan.extract import wechat_gate as gate


class GateTableTest(unittest.TestCase):
    def test_known_versions_hit_table(self):
        # 版本表命中的版本不能去扫描 DLL（几十秒），必须直接返回
        self.assertEqual(gate.candidate_gate_rvas(r"C:\x\4.1.15.53\Weixin.dll"), [0x0B3959D8])
        self.assertEqual(gate.candidate_gate_rvas(r"C:\x\4.1.13.65\Weixin.dll"), [0x0AE2B0C8])

    def test_version_parse(self):
        self.assertEqual(gate.version_of_dll_path(r"D:\a\4.1.15.53\Weixin.dll"), "4.1.15.53")


class GateKeeperTest(unittest.TestCase):
    def test_status_before_start(self):
        keeper = gate.GateKeeper()
        status = keeper.status()
        self.assertFalse(status["alive"])
        self.assertIsNone(status["pid"])
        self.assertEqual(status["writes"], 0)

    def test_keeper_starts_and_stops_cleanly(self):
        keeper = gate.GateKeeper(interval=0.1)
        keeper.start()
        self.assertTrue(keeper.alive)
        keeper.stop()
        self.assertFalse(keeper.alive)

    def test_resolve_never_raises_without_wechat(self):
        # 微信没开时只能记 last_error，不能抛异常（守护线程会带崩主程序）
        keeper = gate.GateKeeper()
        original = gate.wechat_window_handles
        gate.wechat_window_handles = lambda: []
        try:
            self.assertFalse(keeper._resolve())
            self.assertIn("没找到微信主窗口", keeper.last_error)
        finally:
            gate.wechat_window_handles = original


if __name__ == "__main__":
    unittest.main()
