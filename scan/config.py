"""集中配置：YAML + 环境变量覆盖，启动时一次性校验。

设计原则（与 ddwx-bridge 保持一致）：
- 所有可变项都进配置，代码里不出现魔法数字；
- 配置错误在**启动时**就炸掉，不要等到半夜扫描到一半才发现端口写错；
- 环境变量优先级高于 YAML，便于临时排障（STO_SERVER_PORT=9999 ...）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "server": {
        # 8765 是 ddwx-bridge 的网关端口，本项目**不许复用**
        "host": "0.0.0.0",
        "port": 8766,
        "token": "",
        # 只允许本机访问时设 false；手机要连必须 true
        "allow_lan": True,
    },
    "scan": {
        # 77x = 申通；621 = 丹鸟（实测该群 30 个单号全是 6212 开头 15 位）
        "prefixes": ["77", "621"],
        "lengths": [12, 13, 15],
        "regex": "",
        # 真实揽收件也保留人工确认闸：没有"开始"动作就不发任务
        "require_confirm": True,
        "auto_mode": True,      # 自动开批：采集到新单就自动下发，不用点「开始本批」
        "session_max": 500,     # 申行者单次最多 500 条，超出自动拆到下一批
        "batch_max": 500,       # 与申行者上限对齐，界面不再让人填
        # 总开关：关着就什么都不做（不读微信、不下发），开着全自动跑
        # 服务总开关默认**开**：这是无人值守的采集程序，开机就该干活。
        # 以前默认 false，每次重装/换机都变成「服务未开启」，界面上还得手点一次
        # 「开启服务」，否则采集开关被总开关压成关 —— 「重启后采集没反应」的真凶。
        "service_running": True,
        "max_attempts": 2,
        "poll_interval_ms": 2000,
    },
    "excel": {
        "inbox_dir": "inbox",
    },
    "wechat": {
        "enabled": True,
        "group_name": "",
        "poll_ms": 2500,
        "max_messages": 60,
        # 微信接收目录，留空则自动探测 ~/Documents/xwechat_files
        "file_root": "",
        # 只采哪个时间段的消息：today（仅当天）/ range（指定区间）/ all（不限）
        "window": "today",
        "window_start": "",
        "window_end": "",
        # 采集间隔（秒）：0 = 实时（每 2.5 秒看一次）；600 = 10 分钟采一次。
        # 默认 60 秒：够快，又不会一直去碰微信窗口（切群会挪鼠标）。
        "interval_sec": 60,
    },
    "phone": {
        # 程序启动时自动拉起手机监听（tools/phone_watch.py）。
        # 关掉就只能用 tools/run_all.py 手动托管 —— 现场反馈过「只开程序窗口，
        # 结果采集到了手机不动」，就是这里没兜住的场景，所以默认开。
        "auto_watch": True,
        # 监听进程查队列的间隔（秒）
        "watch_interval": 10,
        # 指定设备序列号，留空自动挑第一台已授权真机
        "serial": "",
        # 同机的「钉钉微信转发桥」目录：揽收前要让它暂停巡检，免得抢手机前台。
        # 留空则自动探测（程序根附近 / D:\wxauto-main\ddwx-bridge）。
        "bridge_dir": "",
    },
    "paths": {
        "db": "data/scan.db",
        "logs": "data/logs",
    },
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    token: str
    allow_lan: bool


@dataclass(frozen=True)
class ScanConfig:
    prefixes: tuple[str, ...]
    lengths: tuple[int, ...]
    regex: str
    require_confirm: bool
    batch_max: int
    max_attempts: int
    poll_interval_ms: int
    auto_mode: bool = True   # 默认自动开批：人工只需启动一次程序
    session_max: int = 500   # 申行者单次扫描上限
    # 服务总开关：开着就全自动（盯群 → 入库 → 下发），关着完全待机。
    # **默认开**：这是无人值守程序，开机即干活。关闭后界面会写回 config.yaml，
    # 下次启动沿用（人主动关掉的要尊重）。
    service_running: bool = True


@dataclass(frozen=True)
class ExcelConfig:
    inbox_dir: Path


@dataclass(frozen=True)
class WeChatConfig:
    enabled: bool
    group_name: str
    poll_ms: int
    max_messages: int
    file_root: Path | None = None
    window: str = "today"
    window_start: str = ""
    window_end: str = ""
    # 采集间隔（秒）。0 = 实时轮询；>0 = 每 N 秒才去读一次微信。
    interval_sec: int = 0


@dataclass(frozen=True)
class PhoneConfig:
    """手机监听：程序启动时自己把 tools/phone_watch.py 拉起来，不靠外部托管。"""

    auto_watch: bool = True
    watch_interval: int = 10
    serial: str = ""
    bridge_dir: str = ""


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    log_dir: Path
    server: ServerConfig
    scan: ScanConfig
    excel: ExcelConfig
    wechat: WeChatConfig
    phone: PhoneConfig = field(default_factory=PhoneConfig)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def with_overrides(self, **kw: Any) -> "Config":
        return replace(self, **kw)


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict[str, Any]:
    """STO_SERVER_PORT / STO_SCAN_BATCH_MAX 这类扁平变量 → 嵌套字典。"""
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith("STO_"):
            continue
        parts = [p.lower() for p in key[4:].split("_")]
        if len(parts) < 2:
            continue
        section, name = parts[0], "_".join(parts[1:])
        out.setdefault(section, {})[name] = value
    return out


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数，当前值：{value!r}") from exc


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    raise ConfigError(f"{name} 必须是布尔值，当前值：{value!r}")


def _as_str_list(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigError(f"{name} 必须是非空列表")
    return tuple(str(v).strip() for v in value if str(v).strip())


def _as_window(value: Any) -> str:
    text = str(value or "today").strip().lower()
    if text not in ("today", "range", "all"):
        raise ConfigError("wechat.window 只能是 today / range / all")
    return text


def _as_interval(value: Any) -> int:
    """采集间隔：0 表示实时；其余按秒存，最大 2 小时（防止填个离谱值等一天）。"""
    seconds = _as_int(value if value not in (None, "") else 0, "wechat.interval_sec")
    if seconds < 0:
        raise ConfigError("wechat.interval_sec 不能为负（0 表示实时）")
    if seconds > 7200:
        raise ConfigError("wechat.interval_sec 最大 7200 秒（2 小时）")
    return seconds


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    file_data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            file_data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件解析失败 {cfg_path}: {exc}") from exc
        if not isinstance(file_data, dict):
            raise ConfigError(f"配置文件顶层必须是映射：{cfg_path}")

    data = _deep_merge(_deep_merge(DEFAULTS, file_data), _env_overrides())

    server_raw, scan_raw = data["server"], data["scan"]
    excel_raw, wechat_raw = data["excel"], data["wechat"]
    phone_raw = data.get("phone") or {}
    paths_raw = data["paths"]

    token = str(server_raw.get("token") or "").strip()
    allow_lan = _as_bool(server_raw.get("allow_lan", True), "server.allow_lan")
    if allow_lan and len(token) < 16:
        raise ConfigError(
            "server.token 至少 16 位。手机要凭这个 token 拉任务，"
            "空 token 等于把扫描队列敞开给整个局域网。"
        )

    port = _as_int(server_raw.get("port", 8766), "server.port")
    if not (1 <= port <= 65535):
        raise ConfigError(f"server.port 越界：{port}")
    if port == 8765:
        raise ConfigError("8765 是 ddwx-bridge 的网关端口，本项目不许复用，换一个。")

    prefixes = _as_str_list(scan_raw.get("prefixes"), "scan.prefixes")
    for p in prefixes:
        if not p.isdigit():
            raise ConfigError(f"scan.prefixes 只能是数字：{p!r}")

    lengths = tuple(_as_int(v, "scan.lengths") for v in scan_raw.get("lengths") or [])
    if not lengths or any(v <= 0 for v in lengths):
        raise ConfigError("scan.lengths 必须全是正整数")

    batch_max = _as_int(scan_raw.get("batch_max", 50), "scan.batch_max")
    if batch_max <= 0:
        raise ConfigError("scan.batch_max 必须大于 0")

    max_attempts = _as_int(scan_raw.get("max_attempts", 2), "scan.max_attempts")
    if max_attempts < 0:
        raise ConfigError("scan.max_attempts 不能为负")

    def _p(rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p

    cfg = Config(
        root=ROOT,
        db_path=_p(paths_raw.get("db", "data/scan.db")),
        log_dir=_p(paths_raw.get("logs", "data/logs")),
        server=ServerConfig(
            host=str(server_raw.get("host", "0.0.0.0")),
            port=port,
            token=token,
            allow_lan=allow_lan,
        ),
        scan=ScanConfig(
            prefixes=prefixes,
            lengths=lengths,
            regex=str(scan_raw.get("regex") or "").strip(),
            require_confirm=_as_bool(scan_raw.get("require_confirm", True), "scan.require_confirm"),
            auto_mode=_as_bool(scan_raw.get("auto_mode", True), "scan.auto_mode"),
            session_max=_as_int(scan_raw.get("session_max", 500), "scan.session_max"),
            service_running=_as_bool(
                scan_raw.get("service_running", True), "scan.service_running"
            ),
            batch_max=batch_max,
            max_attempts=max_attempts,
            poll_interval_ms=_as_int(scan_raw.get("poll_interval_ms", 2000), "scan.poll_interval_ms"),
        ),
        excel=ExcelConfig(inbox_dir=_p(excel_raw.get("inbox_dir", "inbox"))),
        wechat=WeChatConfig(
            enabled=_as_bool(wechat_raw.get("enabled", False), "wechat.enabled"),
            group_name=str(wechat_raw.get("group_name") or "").strip(),
            poll_ms=_as_int(wechat_raw.get("poll_ms", 2500), "wechat.poll_ms"),
            max_messages=_as_int(wechat_raw.get("max_messages", 60), "wechat.max_messages"),
            file_root=(
                _p(wechat_raw["file_root"])
                if str(wechat_raw.get("file_root") or "").strip()
                else None
            ),
            window=_as_window(wechat_raw.get("window", "today")),
            window_start=str(wechat_raw.get("window_start") or "").strip(),
            window_end=str(wechat_raw.get("window_end") or "").strip(),
            interval_sec=_as_interval(wechat_raw.get("interval_sec", 0)),
        ),
        phone=PhoneConfig(
            auto_watch=_as_bool(phone_raw.get("auto_watch", True), "phone.auto_watch"),
            watch_interval=max(3, _as_int(phone_raw.get("watch_interval", 10), "phone.watch_interval")),
            serial=str(phone_raw.get("serial") or "").strip(),
            bridge_dir=str(phone_raw.get("bridge_dir") or "").strip(),
        ),
        raw=data,
    )

    # 目录在启动时就建好，别等写入时才报错
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.excel.inbox_dir.mkdir(parents=True, exist_ok=True)
    return cfg
