"""扫描规则（前缀 + 长度）：界面上可改，改完立刻生效。

为什么不能只写 config.yaml：改文件要重启进程，业务员不会去重启。
为什么不能只改内存：重启就丢。
所以双写——
  runtime 表   → 当前进程立即生效（采集线程每轮都重新读）
  config.yaml  → 下次启动仍然是这个值（原子写，避免写一半崩掉）

读取时 runtime 优先：它在界面上被人显式改过，比文件更"新"。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from .db import connect, get_runtime, set_runtime
from .errors import ValidationError
from .log import get_logger

log = get_logger("scan.rules")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

_PREFIX_RE = re.compile(r"^\d{1,4}$")
MAX_LENGTH = 30
# 采集间隔上限：2 小时。再长就没意义了（真不想采就把服务停了）
MAX_INTERVAL_SEC = 7200


def _iter_items(values: Iterable[Any]) -> list[Any]:
    """字符串是特例：它本身可迭代，但「77, 621」必须当成一个整体，
    否则会被拆成 '7' '7' ',' ' ' '6' '2' '1'。"""
    if isinstance(values, (str, bytes)):
        return [values]
    return list(values)


def _check_date(value: str) -> str:
    """只接受 YYYY-MM-DD；空串表示不限。返回规范化后的字符串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValidationError(f"日期「{text}」格式不对，应为 2026-10-19 这种") from None


def normalize_prefixes(values: Iterable[Any]) -> tuple[str, ...]:
    """'77' / '77, 621' / ['77','621'] → ('77','621')，去重保序。"""
    out: list[str] = []
    for raw in _iter_items(values):
        for piece in str(raw).replace("，", ",").split(","):
            item = piece.strip()
            if not item:
                continue
            if not _PREFIX_RE.match(item):
                raise ValidationError(
                    f"前缀「{item}」不合法：只能是 1–4 位数字，比如 77、621"
                )
            if item not in out:
                out.append(item)
    return tuple(out)


def normalize_lengths(values: Iterable[Any]) -> tuple[int, ...]:
    out: set[int] = set()
    for raw in _iter_items(values):
        for piece in str(raw).replace("，", ",").split(","):
            item = piece.strip()
            if not item:
                continue
            if not item.isdigit():
                raise ValidationError(f"位数「{item}」不合法：只能是正整数，比如 12、15")
            number = int(item)
            if not 1 <= number <= MAX_LENGTH:
                raise ValidationError(f"位数「{item}」不合法：应在 1–{MAX_LENGTH} 之间")
            out.add(number)
    return tuple(sorted(out))


def _check_interval(value: Any) -> int:
    """采集间隔（秒）：0 = 实时。只允许整数，范围 0–7200。"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValidationError("采集间隔必须是整数秒（0 表示实时）") from None
    if not 0 <= seconds <= MAX_INTERVAL_SEC:
        raise ValidationError(
            f"采集间隔应在 0–{MAX_INTERVAL_SEC} 秒之间（0 表示实时，600 表示 10 分钟）"
        )
    return seconds


class RuleStore:
    """线程安全的规则持有者。采集线程每轮调用属性，拿到的是最新值。"""

    KEY = "scan.rules"
    WECHAT_KEY = "wechat.settings"
    LIMITS_KEY = "scan.limits"
    SERVICE_KEY = "service.state"

    def __init__(
        self, config, db_path: Path | None = None, config_path: Path | None = None
    ) -> None:
        self.config = config
        self.db_path = Path(db_path or config.db_path)
        # 显式可注入：测试绝不能写回真实的 config.yaml
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self._lock = threading.RLock()
        self._prefixes: tuple[str, ...] = tuple(config.scan.prefixes)
        self._lengths: tuple[int, ...] = tuple(config.scan.lengths)
        self._group_name: str = config.wechat.group_name or ""
        self._enabled: bool = bool(config.wechat.enabled)
        # 采集时间范围：today（只当天）/ range（区间）/ all（不限）
        self._window: str = getattr(config.wechat, "window", "today")
        self._window_start: str = getattr(config.wechat, "window_start", "")
        self._window_end: str = getattr(config.wechat, "window_end", "")
        # 采集间隔（秒）：0 = 实时；>0 = 每 N 秒才去读一次微信。
        # 自动切群/翻会话列表会挪动鼠标，间隔是给"别老抢我鼠标"留的开关。
        self._interval_sec: int = int(getattr(config.wechat, "interval_sec", 0) or 0)
        self._batch_max: int = int(config.scan.batch_max)
        self._max_attempts: int = int(config.scan.max_attempts)
        # 自动模式：采集到新单就自动开批下发，人工只需启动一次程序
        self._auto: bool = bool(config.scan.auto_mode)
        # 服务总开关：关着 = 完全待机（不读微信、不开批）；开着 = 全自动。
        # 默认 **True**：无人值守程序，开机就该干活；人主动关掉会写回 config.yaml 沿用。
        self._running: bool = bool(getattr(config.scan, "service_running", True))
        self._source = "config"
        self._reload()

    # ---------- 读 ----------

    def _reload(self) -> None:
        try:
            with connect(self.db_path) as conn:
                raw = get_runtime(conn, self.KEY, "")
                raw_group = get_runtime(conn, self.WECHAT_KEY, "")
                raw_limits = get_runtime(conn, self.LIMITS_KEY, "")
                raw_service = get_runtime(conn, self.SERVICE_KEY, "")
        except Exception:  # noqa: BLE001 - 库还没建时退回文件配置
            return
        if raw_group:
            try:
                data = json.loads(raw_group)
                group = data.get("group_name")
                if isinstance(group, str):
                    self._group_name = group
                if isinstance(data.get("enabled"), bool):
                    self._enabled = data["enabled"]
                if data.get("window") in ("today", "range", "all"):
                    self._window = data["window"]
                if isinstance(data.get("window_start"), str):
                    self._window_start = data["window_start"]
                if isinstance(data.get("window_end"), str):
                    self._window_end = data["window_end"]
                if isinstance(data.get("interval_sec"), int) and data["interval_sec"] >= 0:
                    self._interval_sec = data["interval_sec"]
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        if raw_limits:
            try:
                limits = json.loads(raw_limits)
                if isinstance(limits.get("batch_max"), int):
                    self._batch_max = limits["batch_max"]
                if isinstance(limits.get("max_attempts"), int):
                    self._max_attempts = limits["max_attempts"]
                if isinstance(limits.get("auto_mode"), bool):
                    self._auto = limits["auto_mode"]
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        # 总开关只做「临时压制」，不改写用户设置的采集开关。
        # 以前这里把 _enabled 直接赋值成 _running：一旦服务停过一次，
        # 采集开关就被永久写成 false，下次开服务还得手工再开一次采集
        # ——「重启后采集没反应」的第二个真凶。
        if raw_service:
            try:
                state = json.loads(raw_service)
                if isinstance(state.get("running"), bool):
                    self._running = state["running"]
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        if not raw:
            return
        try:
            data = json.loads(raw)
            prefixes = tuple(str(x) for x in data.get("prefixes") or [])
            lengths = tuple(int(x) for x in data.get("lengths") or [])
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not prefixes and not lengths:
            return
        self._prefixes, self._lengths, self._source = prefixes, lengths, "runtime"

    @property
    def prefixes(self) -> tuple[str, ...]:
        with self._lock:
            return self._prefixes

    @property
    def lengths(self) -> tuple[int, ...]:
        with self._lock:
            return self._lengths

    @property
    def group_name(self) -> str:
        """要采集的微信群名。空串 = 还没配，采集线程会跳过并提示。"""
        with self._lock:
            return self._group_name

    @property
    def enabled(self) -> bool:
        """当前是否真的要去读微信 = 「启用采集」开关 AND「服务总开关」。

        分开看是关键：总开关关闭只是临时压制，不能把用户设置的
        「启用采集」也改掉（否则开回服务后还要手工再开一次采集）。
        界面要显示用户意图时读 snapshot()["enabled"]，读这个属性才是「此刻干不干活」。
        """
        with self._lock:
            return self._enabled and self._running

    @property
    def window(self) -> tuple[str, str, str]:
        """(模式, 起始日, 结束日)。模式：today / range / all。"""
        with self._lock:
            return self._window, self._window_start, self._window_end

    @property
    def interval_sec(self) -> int:
        """采集间隔（秒）。0 = 实时；>0 = 每 N 秒读一次微信。"""
        with self._lock:
            return self._interval_sec

    @property
    def batch_max(self) -> int:
        with self._lock:
            return self._batch_max

    @property
    def max_attempts(self) -> int:
        with self._lock:
            return self._max_attempts

    @property
    def auto_mode(self) -> bool:
        with self._lock:
            return self._auto

    @property
    def running(self) -> bool:
        """服务总开关。关着 = 完全待机：不读微信、不开批、不下发。"""
        with self._lock:
            return self._running

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "prefixes": list(self._prefixes),
                "lengths": list(self._lengths),
                "group_name": self._group_name,
                "enabled": self._enabled,
                "window": self._window,
                "window_start": self._window_start,
                "window_end": self._window_end,
                "interval_sec": self._interval_sec,
                "batch_max": self._batch_max,
                "max_attempts": self._max_attempts,
                "auto_mode": self._auto,
                "session_max": int(getattr(self.config.scan, "session_max", 500) or 500),
                "running": self._running,
                "source": self._source,
            }

    # ---------- 写 ----------

    def update(self, prefixes: Iterable[Any], lengths: Iterable[Any]) -> dict[str, Any]:
        new_prefixes = normalize_prefixes(prefixes)
        new_lengths = normalize_lengths(lengths)
        if not new_prefixes and not new_lengths:
            raise ValidationError("前缀和位数不能同时为空，否则会把所有数字串都当单号")

        with self._lock:
            self._prefixes, self._lengths = new_prefixes, new_lengths
            self._source = "runtime"
            payload = json.dumps(
                {"prefixes": list(new_prefixes), "lengths": list(new_lengths)},
                ensure_ascii=False,
            )
            with connect(self.db_path) as conn:
                set_runtime(conn, self.KEY, payload)
            self._persist(new_prefixes, new_lengths)

        log.info(
            "扫描规则已更新",
            extra={"status": f"prefixes={new_prefixes} lengths={new_lengths}"},
        )
        return self.snapshot()

    def update_group(
        self,
        group_name: Any,
        enabled: bool | None = None,
        window: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        interval_sec: Any = None,
    ) -> dict[str, Any]:
        """改采集群 + 采集时间范围 + 采集间隔。

        空群名 = 暂不采集；window 取 today / range / all。
        「只扫当天的」是默认，因为群里历史单再扫一遍会产生真实轨迹。
        interval_sec：0 = 实时（2.5 秒一次）；600 = 10 分钟一次。
        间隔越长，自动切群/翻会话列表挪鼠标的次数越少 —— 要干别的活时调大。
        """
        if group_name is None:
            raise ValidationError("请提供 group_name（微信群名）")
        name = re.sub(r"\s+", " ", str(group_name)).strip()
        if len(name) > 100:
            raise ValidationError("群名太长了（超过 100 字），请只填群名本身")

        mode = window or self._window
        if mode not in ("today", "range", "all"):
            raise ValidationError("采集时间范围只能是 today（仅当天）/ range（指定区间）/ all（不限）")
        start = _check_date(window_start) if window_start is not None else self._window_start
        end = _check_date(window_end) if window_end is not None else self._window_end
        if mode == "range":
            if not start or not end:
                raise ValidationError("选了「指定区间」就要填开始和结束日期")
            if start > end:
                raise ValidationError(f"开始日期 {start} 晚于结束日期 {end}")
        interval = (
            _check_interval(interval_sec)
            if interval_sec is not None
            else self._interval_sec
        )

        with self._lock:
            self._group_name = name
            self._window, self._window_start, self._window_end = mode, start, end
            self._interval_sec = interval
            if enabled is not None:
                self._enabled = bool(enabled)
            payload = json.dumps(
                {
                    "group_name": name,
                    "enabled": self._enabled,
                    "window": self._window,
                    "window_start": self._window_start,
                    "window_end": self._window_end,
                    "interval_sec": self._interval_sec,
                },
                ensure_ascii=False,
            )
            with connect(self.db_path) as conn:
                set_runtime(conn, self.WECHAT_KEY, payload)
            self._persist(
                self._prefixes,
                self._lengths,
                group=name,
                enabled=enabled,
                window=mode,
                window_start=start,
                window_end=end,
                interval_sec=interval,
            )

        log.info(
            "采集设置已更新",
            extra={
                "status": (
                    f"group={name or '(未设置)'} window={mode} {start}~{end} "
                    f"interval={interval}s"
                )
            },
        )
        return self.snapshot()

    def update_limits(
        self, batch_max: Any, max_attempts: Any, auto_mode: bool | None = None
    ) -> dict[str, Any]:
        """单批上限 / 失败重试次数。改完立即生效（服务端每次读的是本对象）。"""
        try:
            new_batch = int(batch_max)
            new_attempts = int(max_attempts)
        except (TypeError, ValueError):
            raise ValidationError("单批上限和重试次数都必须是整数") from None
        if not 1 <= new_batch <= 5000:
            raise ValidationError("单批上限应在 1–5000 之间")
        if not 0 <= new_attempts <= 10:
            raise ValidationError("失败重试次数应在 0–10 之间")

        with self._lock:
            self._batch_max, self._max_attempts = new_batch, new_attempts
            if auto_mode is not None:
                self._auto = bool(auto_mode)
            payload = json.dumps(
                {
                    "batch_max": new_batch,
                    "max_attempts": new_attempts,
                    "auto_mode": self._auto,
                "session_max": int(getattr(self.config.scan, "session_max", 500) or 500),
                }
            )
            with connect(self.db_path) as conn:
                set_runtime(conn, self.LIMITS_KEY, payload)
            self._persist(
                self._prefixes,
                self._lengths,
                batch_max=new_batch,
                max_attempts=new_attempts,
                auto_mode=self._auto,
            )

        log.info("入库边界已更新", extra={"status": f"batch_max={new_batch} attempts={new_attempts}"})
        return self.snapshot()

    def set_running(self, running: bool, actor: str = "operator") -> dict[str, Any]:
        """开 / 关服务总开关。

        开着：采集线程开始盯群 → 新单入队 → 有积压就自动开批下发，
              手机连上就能领任务，全程不用再点任何按钮。
        关着：采集线程原地待命，也不自动开批（已经发出去的批次不受影响，
              要停下正在扫的那批用「中止本批」）。
        """
        if not isinstance(running, bool):
            raise ValidationError("running 必须是 true / false")
        with self._lock:
            self._running = running
            # 不动 self._enabled：总开关只是临时压制采集，
            # 用户自己设的「启用采集」要保留下来，开回服务就恢复采集。
            payload = json.dumps({"running": running}, ensure_ascii=False)
            with connect(self.db_path) as conn:
                set_runtime(conn, self.SERVICE_KEY, payload)
            self._persist(
                self._prefixes,
                self._lengths,
                service_running=running,
            )
        log.info(
            "服务总开关" + ("已开启" if running else "已停止"),
            extra={"status": f"actor={actor} group={self._group_name or '(未设置)'}"},
        )
        return self.snapshot()

    def _persist(
        self,
        prefixes: tuple[str, ...],
        lengths: tuple[int, ...],
        group: str | None = None,
        enabled: bool | None = None,
        window: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        interval_sec: int | None = None,
        batch_max: int | None = None,
        max_attempts: int | None = None,
        auto_mode: bool | None = None,
        service_running: bool | None = None,
    ) -> None:
        """写回 config.yaml。原子替换，避免中途崩溃留下半个文件。"""
        path = self.config_path
        try:
            data: dict[str, Any] = {}
            if path.exists():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(data, dict):
                    data = {}
            data.setdefault("scan", {})
            data["scan"]["prefixes"] = list(prefixes)
            data["scan"]["lengths"] = list(lengths)
            if batch_max is not None:
                data["scan"]["batch_max"] = int(batch_max)
            if max_attempts is not None:
                data["scan"]["max_attempts"] = int(max_attempts)
            if auto_mode is not None:
                data["scan"]["auto_mode"] = bool(auto_mode)
            if service_running is not None:
                data["scan"]["service_running"] = bool(service_running)
            if group is not None or enabled is not None or window is not None or interval_sec is not None:
                data.setdefault("wechat", {})
                if group is not None:
                    data["wechat"]["group_name"] = group
                if enabled is not None:
                    data["wechat"]["enabled"] = bool(enabled)
                if window is not None:
                    data["wechat"]["window"] = window
                    data["wechat"]["window_start"] = window_start or ""
                    data["wechat"]["window_end"] = window_end or ""
                if interval_sec is not None:
                    data["wechat"]["interval_sec"] = int(interval_sec)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不该让界面报错
            log.error("规则写回 config.yaml 失败", extra={"status": str(exc)})
