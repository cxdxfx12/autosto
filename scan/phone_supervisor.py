"""手机监听守护：程序自己把「手机揽收监听」拉起来，崩了自动重起。

为什么要它
----------
以前手机监听靠外部 `tools/run_all.py` 托管。现场只双击程序窗口时，
监听没起来 —— 表现出来就是「微信采到了、队列有积压，手机一动不动」。
（2026-10-27 实际踩到：批次开出来 1061 条，没有任何进程去驱动手机。）

设计
----
* 监听跑在**独立子进程**里：UIA/adb 那条链路异常多，绝不能把后端一起带走；
* 子进程就是本程序自己 —— 源码态 `python tools/phone_watch.py`，
  冻结态 `miao-core.exe --phone-watch`（参数分流在 core_main 里），
  这样不用往包里塞一个 Python 解释器；
* 日志由子进程自己按 UTF-8 写 `data/logs/phone_watch.log`。**不能**靠重定向
  stdout：windowed 打包后那段 stdout 跟着系统代码页（GBK）走，会整篇乱码；
* 守护线程每 10s 看一眼，退出就重起；短时间内连续崩 3 次就冷却 60s，
  避免手机没插、adb 报错时疯狂拉起刷日志。
"""

from __future__ import annotations

import atexit
import locale
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .log import get_logger

log = get_logger("scan.phone")

TICK = 10.0            # 守护线程检查间隔
FAST_CRASH = 30.0      # 存活时间短于这个值算「启动失败」
MAX_FAST_CRASH = 3     # 连续启动失败几次后冷却
COOLDOWN = 60.0        # 冷却时长（秒）
TAIL_LINES = 40        # 崩溃时回捞的子进程输出行数


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


class PhoneSupervisor:
    def __init__(
        self,
        base: Path,
        *,
        interval: int = 10,
        token: str = "",
        serial: str = "",
        bridge_dir: str = "",
        log_dir: Path | None = None,
    ) -> None:
        self.base = base
        self.interval = interval
        self.token = token
        self.serial = serial
        self.bridge_dir = bridge_dir
        self.log_dir = log_dir or (base / "data" / "logs")
        self._proc: subprocess.Popen | None = None
        self._tail: deque[str] = deque(maxlen=TAIL_LINES)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fast_crashes = 0
        self._cooldown_until = 0.0
        self._started_at = 0.0

    # ---------- 子进程 ----------

    def _adb_dir(self) -> Path | None:
        for cand in (self.base / "vendor" / "platform-tools", self.base / "platform-tools"):
            if (cand / "adb.exe").exists():
                return cand
        return None

    def _cmd(self) -> list[str] | None:
        if _frozen():
            # 冻结态：本 exe 带 --phone-watch 参数重新进来，由 core_main 分流到 tools
            return [sys.executable, "--phone-watch", "--interval", str(self.interval)]
        script = self.base / "tools" / "phone_watch.py"
        if not script.exists():
            return None
        return [sys.executable, str(script), "--interval", str(self.interval)]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        # 冻结后 tools/*.py 的 ROOT 推不出来（在 _internal 里），用环境变量兜住
        env["MIAO_ROOT"] = str(self.base)
        # token 只在冻结态显式下发：安装版的 config 和它自己起的后端必然一致。
        # 源码态（开发调试）反而不能盖 —— phone._token() 会按「谁在监听 8766」
        # 去反查那个目录的 config.yaml；硬塞开发库 token 连安装版会 401。
        if _frozen() and self.token:
            env["STO_SERVER_TOKEN"] = self.token
        if self.serial:
            env["ANDROID_SERIAL"] = self.serial
        if self.bridge_dir:
            # 安装版推导不出「兄弟目录」，显式告诉让路模块去哪读 token
            p = Path(self.bridge_dir)
            env["STO_BRIDGE_CONFIG"] = str(p / "config.yaml" if p.is_dir() else p)
        adb = self._adb_dir()
        if adb is not None:
            env["PATH"] = str(adb) + os.pathsep + env.get("PATH", "")
        # 强制 UTF-8：windowed 子进程默认按系统 ANSI（GBK）写 stdout，
        # 日志文件会变 GBK，tail / 编辑器里全是乱码，排查时非常费劲。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def start(self) -> bool:
        cmd = self._cmd()
        if cmd is None:
            log.warning("找不到手机监听脚本，跳过自动拉起")
            return False
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self.base),
                env=self._env(),
                # 日志由子进程自己按 UTF-8 落盘（data/logs/phone_watch.log）。
                # 这里接管道只为两件事：不写满缓冲区卡住子进程、崩溃时留个尾巴。
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log.warning(f"手机监听启动失败：{exc}")
            return False
        self._tail.clear()
        threading.Thread(
            target=self._pump, args=(self._proc,), name="phone-watch-pump", daemon=True
        ).start()
        self._started_at = time.time()
        log.info("手机监听已自动启动", extra={"status": f"pid={self._proc.pid}"})
        return True

    def _pump(self, proc: subprocess.Popen) -> None:
        """把子进程输出读进环形缓冲（不落盘），并保证管道不被写满。"""
        stream = proc.stdout
        if stream is None:
            return
        enc = locale.getpreferredencoding(False) or "utf-8"
        try:
            for raw in iter(stream.readline, b""):
                self._tail.append(raw.decode(enc, "replace").rstrip())
        except Exception:  # noqa: BLE001 —— 管道关闭时正常退出
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---------- 守护 ----------

    def _guard(self) -> None:
        while not self._stop.wait(TICK):
            if self._alive():
                # 活过 30s 就认为「正常启动过」，清零失败计数
                if time.time() - self._started_at > FAST_CRASH:
                    self._fast_crashes = 0
                continue

            code = self._proc.returncode if self._proc is not None else None
            lived = time.time() - self._started_at
            if code == 0 and lived < FAST_CRASH:
                # 退出码 0 且没活多久 = 被另一个监听实例挡住了（单实例锁）。
                # 这不算崩溃：用户可能还留着 tools/run_all.py 托管的那个。
                if time.time() >= self._cooldown_until:
                    log.info("手机监听已由其它实例接管，60 秒后再探一次")
                self._cooldown_until = time.time() + COOLDOWN
                continue
            if lived < FAST_CRASH:
                self._fast_crashes += 1
                if self._fast_crashes >= MAX_FAST_CRASH:
                    self._cooldown_until = time.time() + COOLDOWN
                    self._fast_crashes = 0
                    log.warning(
                        "手机监听连续启动失败，暂停 60 秒",
                        extra={"status": f"exit={code}"},
                    )
            if time.time() < self._cooldown_until:
                continue
            if lived < FAST_CRASH and self._tail:
                # 启动就崩的情况：把子进程最后几行带出来，否则现场只能看到一个退出码
                for line in list(self._tail)[-8:]:
                    log.warning("  | " + line)
            log.info("手机监听退出，重新拉起", extra={"status": f"exit={code} lived={int(lived)}s"})
            self.start()

    def start_in_background(self) -> None:
        if self._thread is not None:
            return
        if not self.start():
            return
        self._thread = threading.Thread(target=self._guard, name="phone-supervisor", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001 —— 收尾绝不抛
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # 只收自己拉起来的那个：上一轮被强杀的残留监听会因为「队列已被别人接走」
        # 而自然空转，不去硬杀 —— 按名字杀进程会误伤用户手动开的调试实例。
        self._proc = None


def start(config) -> PhoneSupervisor | None:
    """按配置决定是否自动托管手机监听。返回实例（未启用则为 None）。"""
    phone_cfg = getattr(config, "phone", None)
    if phone_cfg is None or not phone_cfg.auto_watch:
        log.info("手机监听未启用（config.yaml 里 phone.auto_watch = false）")
        return None
    sup = PhoneSupervisor(
        config.root,
        interval=phone_cfg.watch_interval,
        token=config.server.token,
        serial=phone_cfg.serial,
        bridge_dir=getattr(phone_cfg, "bridge_dir", ""),
        log_dir=config.log_dir,
    )
    sup.start_in_background()
    return sup
