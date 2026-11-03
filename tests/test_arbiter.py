"""phone_arbiter 的测试：让路请求的形状、失败降级、token 解析。

这里**不打真网络**：桩掉 urllib 只验我们发出去的东西对不对，以及
「对方没开」时绝不能把揽收带崩 —— 那是这个模块存在的底线。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import phone_arbiter as pa  # noqa: E402


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class ArbiterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _patch_config(self, text: str) -> None:
        cfg = self.tmp / "config.yaml"
        cfg.write_text(text, encoding="utf-8")
        p = mock.patch.object(pa, "CONFIG_CANDIDATES", (cfg,))
        p.start()
        self.addCleanup(p.stop)

    def test_token_read(self) -> None:
        """gateway_token 要能取到 —— 取不到就是 401，对方当成攻击。"""
        self._patch_config('dingtalk:\n  gateway_token: "abc-123"\n')
        self.assertEqual(pa._peer_token(), "abc-123")

    def test_token_missing_is_empty_not_crash(self) -> None:
        self._patch_config("dingtalk:\n  mode: android\n")
        self.assertEqual(pa._peer_token(), "")

    def test_token_ignores_comments(self) -> None:
        self._patch_config('# gateway_token: "nope"\n  gateway_token: "real"\n')
        self.assertEqual(pa._peer_token(), "real")

    def test_ask_sends_seconds_and_token(self) -> None:
        """请求体与鉴权头都得对：对方按 seconds 定时长、按头鉴权。"""
        self._patch_config('gateway_token: "tk"\n')
        captured = {}

        def fake_open(_self, req, *a, **k):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["token"] = req.get_header("X-bridge-token")
            return _FakeResp(json.dumps({"ok": True, "message": "已让路"}).encode())

        with mock.patch("urllib.request.OpenerDirector.open", fake_open):
            ok = pa.ask_peer_yield(180)

        self.assertTrue(ok)
        self.assertIn("/watch-pause", captured["url"])
        self.assertEqual(captured["body"], {"seconds": 180})
        self.assertEqual(captured["token"], "tk")

    def test_peer_down_does_not_block(self) -> None:
        """对方没开 → 返回 True，绝不抛异常。揽收不能被隔壁没开挡住。"""
        self._patch_config("")
        with mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=urllib.error.URLError(OSError("拒绝连接"))):
            self.assertTrue(pa.ask_peer_yield(60))

    def test_unauthorized_reports_false(self) -> None:
        """开着但拒了我们 → False，需要人看一眼（多半是 token 改了）。"""
        self._patch_config("")
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=err):
            self.assertFalse(pa.ask_peer_yield(60))

    def test_release_sends_zero(self) -> None:
        """解除就是 seconds=0，不另开接口（少一个能写错的地方）。"""
        self._patch_config("")
        captured = {}

        def fake_open(_self, req, *a, **k):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp(b'{"ok": true, "message": "ok"}')

        with mock.patch("urllib.request.OpenerDirector.open", fake_open):
            pa.release_peer()
        self.assertEqual(captured["body"], {"seconds": 0})

    def test_bypasses_proxy(self) -> None:
        """必须绕代理：127.0.0.1 走系统代理会变成 502（踩过）。"""
        self._patch_config("")
        seen = {}

        real_build = urllib.request.build_opener

        def spy(*handlers):
            seen["handlers"] = handlers
            return real_build(*handlers)

        with mock.patch("urllib.request.build_opener", spy), \
             mock.patch("urllib.request.OpenerDirector.open",
                        side_effect=urllib.error.URLError(OSError("x"))):
            pa.ask_peer_yield(10)
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler)
                            for h in seen["handlers"]))


if __name__ == "__main__":
    unittest.main()
