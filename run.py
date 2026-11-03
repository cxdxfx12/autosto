"""命令行入口。

    python run.py init            生成 config.yaml（内含随机 token）
    python run.py serve           启动服务（默认）
    python run.py import <文件>   导入一份 Excel / CSV
    python run.py check           自检：配置 + 数据库 + 端口占用
"""

from __future__ import annotations

import argparse
import secrets
import socket
import sys
from pathlib import Path

from scan.config import ROOT, Config, ConfigError, load_config
from scan.db import migrate
from scan.log import get_logger, setup_logging
from scan.service import JobService

log = get_logger("scan.cli")

# 微信采集线程只允许起一次（界面上可以随时启用，线程内每轮重读群名/规则）
_WECHAT_STARTED = False
# 采集器实例：HTTP 层要通过它查状态 / 让采集线程列会话、切群。
# 放在模块级是因为它归属「采集线程」而不是某个 HTTP 请求。
_WECHAT_COLLECTOR = None


def wechat_collector():
    """给 HTTP 层拿采集器（可能为 None：还没起过采集线程）。"""
    return _WECHAT_COLLECTOR

CONFIG_TEMPLATE = """\
# 运单揽件助手配置。改完重启服务生效。
# 注意：本文件含 token，已被 .gitignore 排除，不要提交。

server:
  host: 0.0.0.0
  port: 8766              # 8765 是 ddwx-bridge 的，不许复用
  token: "{token}"        # 手机端拉任务要带 X-Token
  allow_lan: true         # 只在本机用可以设 false

scan:
  prefixes: ["77"]        # 运单号前缀，可多个
  lengths: [12, 13, 15]   # 允许长度
  regex: ""               # 高级：自定义正则，留空用上面两条
  require_confirm: false  # 全自动：不需要人工点「开始本批」
  auto_mode: true         # 采集到新单就自动开批下发
  service_running: true   # 服务总开关：开机即自动采集下发；界面上可停止，停止后写回这里
  batch_max: 500          # 与申行者单次上限对齐，超出的自动排到下一批
  max_attempts: 2         # 失败重试次数（含首次）
  poll_interval_ms: 2000  # 手机轮询间隔

excel:
  inbox_dir: inbox        # 表格丢这里，界面点「扫描 inbox 目录」

wechat:
  enabled: false          # 跟随服务总开关
  group_name: ""          # 例如：香飘飘申通揽收
  poll_ms: 2500
  max_messages: 60
  window: today           # 只采当天（today / range / all）
  interval_sec: 60        # 采集间隔：0=实时；300=5分钟；600=10分钟；900=15分钟

paths:
  db: data/scan.db
  logs: data/logs

phone:
  auto_watch: true        # 程序启动时自动拉起手机监听（关掉就只能手动跑 tools/run_all.py）
  watch_interval: 10      # 监听查队列的间隔（秒）
  serial: ""              # 指定手机序列号，留空自动挑第一台已授权真机
  bridge_dir: ""          # 同机「钉钉微信转发桥」目录，揽收前让它暂停巡检；留空自动探测
"""


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path) if args.path else ROOT / "config.yaml"
    if target.exists() and not args.force:
        print(f"{target} 已存在。要覆盖请加 --force")
        return 1
    token = secrets.token_urlsafe(24)
    target.write_text(CONFIG_TEMPLATE.replace("{token}", token), encoding="utf-8")
    print(f"已生成 {target}")
    print(f"手机端 token：{token}")
    print("（token 只在这里显示一次，也可以在文件里查看）")
    return 0


def _boot() -> tuple[Config, JobService]:
    config = load_config()
    setup_logging(config.log_dir)
    migrate(config.db_path)
    return config, JobService(config.db_path, config)


def _start_wechat_thread(config: Config, service: JobService, rules=None) -> None:
    """微信采集放独立线程：UIA 卡住不能拖垮 HTTP 服务。

    uiautomation 在新线程里必须显式初始化 COM，否则第一次调用就会抛
    «CoInitialize has not been called»。官方提供了上下文管理器，直接用它。
    """
    import threading
    import time

    from scan.extract.wechat import WeChatCollector, WeChatUnavailable

    global _WECHAT_STARTED
    if _WECHAT_STARTED:
        return  # 界面上反复点「启用」不该起出第二个线程
    _WECHAT_STARTED = True

    def loop() -> None:
        try:
            import uiautomation as uia
        except ImportError:
            log.error("缺少 uiautomation，微信采集未启动（pip install uiautomation comtypes）")
            return

        with uia.UIAutomationInitializerInThread():
            collector = WeChatCollector(config, service, rules=rules)
            global _WECHAT_COLLECTOR
            _WECHAT_COLLECTOR = collector
            last_reason = ""
            poll_sec = max(500, config.wechat.poll_ms) / 1000

            def serve_pending() -> None:
                """界面发来的「列出会话 / 打开某个群」请求先做掉。

                这些是 UIA 调用，只能在这个线程里跑；采集间隔调到 15 分钟时，
                也必须随时能响应，否则设置中心点「读取微信会话」会一直转圈。
                """
                try:
                    collector.serve_requests()
                except Exception:
                    log.exception("处理界面微信请求失败")

            while True:
                serve_pending()
                # 服务总开关关着就原地待命：不碰微信 UI（那是最重的一步），
                # 也不写日志刷屏。界面点「开启服务」后下一轮立刻恢复。
                if rules is not None and not rules.running:
                    last_reason = ""
                    collector.interval_sec = 0
                    collector.next_at = 0.0
                    time.sleep(1.0)
                    continue

                # 采集间隔：0 = 实时（沿用 poll_ms）；>0 = 每 N 秒才读一次微信。
                # 间隔调大后，自动切群/翻会话列表这些「挪鼠标」的动作也跟着变少。
                interval = int(getattr(rules, "interval_sec", 0) or 0) if rules is not None else 0
                collector.interval_sec = interval
                collector.next_at = 0.0  # 正在采，界面上不显示倒计时

                try:
                    result = collector.poll_once()
                    if result.get("inserted"):
                        log.info("微信采集入队", extra={"status": f"inserted={result['inserted']}"})
                    if last_reason:
                        # 从「暂停」恢复，明确记一条，否则日志里看不出何时恢复的
                        log.info("微信采集恢复", extra={"status": f"group={config.wechat.group_name}"})
                        last_reason = ""
                except WeChatUnavailable as exc:
                    # 窗口被关、切到别的群、微信退化——都不是崩溃，下一轮再试。
                    # 只在原因变化时记一条：否则每几秒刷一次，日志一天几万行。
                    reason = str(exc)
                    if reason != last_reason:
                        log.warning("微信采集暂停：%s", reason)
                        last_reason = reason
                except Exception:
                    log.exception("微信采集异常")

                wait = interval if interval > 0 else poll_sec
                collector.next_at = time.time() + wait
                deadline = time.monotonic() + wait
                # 分段睡：睡了也要能被两件事叫醒 —— 界面请求、间隔被改小/服务被停。
                # 否则设成 15 分钟时，点「切到采集群」要等满 15 分钟才有反应。
                while True:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        break
                    time.sleep(min(0.4, remain))
                    serve_pending()
                    if rules is not None and not rules.running:
                        break
                    if int(getattr(rules, "interval_sec", 0) or 0) != interval:
                        break
                collector.next_at = 0.0

    threading.Thread(target=loop, name="wechat-collector", daemon=True).start()
    log.info("微信采集已启动", extra={"status": f"group={config.wechat.group_name}"})


def _start_auto_thread(config: Config, service: JobService) -> None:
    """自动开批线程：每 3 秒看一次，有积压就自动开批下发。"""
    import threading
    import time

    def loop() -> None:
        while True:
            try:
                batch = service.auto_dispatch()
                if batch:
                    log.info("自动开批", extra={"batch_id": batch.get("id"),
                                               "status": f"n={batch.get('limit_n')}"})
            except Exception:
                log.exception("自动开批失败")
            time.sleep(3)

    threading.Thread(target=loop, name="auto-dispatch", daemon=True).start()
    log.info("自动模式已开启：有单就自动下发，无需手动点开始")


def _acquire_single_instance(config) -> object | None:
    """单实例锁：双开会抢同一个 DB / 端口，还会互相把服务总开关写花。

    第二份程序拿到锁会失败，直接退出并留下明确提示。锁文件在 data/ 下，
    进程一死锁自动释放（msvcrt 是进程级锁，不需要清理）。
    """
    import msvcrt

    lock_path = Path(config.db_path).parent / "app.lock"
    fh = open(lock_path, "a+b")
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh  # 保住句柄，进程退出时系统统一释放


def cmd_serve(_: argparse.Namespace) -> int:
    from scan.rules import RuleStore
    from scan.server import serve

    config, service = _boot()
    lock_fh = _acquire_single_instance(config)
    if lock_fh is None:
        log.error("已有一份程序在运行（锁被占用），本份不启动。请关掉已开的程序或直接使用它，不要双开。")
        return 1
    rules = RuleStore(config)
    log.info(
        "配置加载完成",
        extra={
            "path": f"db={config.db_path}",
            "status": f"prefixes={rules.prefixes} lengths={rules.lengths}",
        },
    )
    # 采集线程常驻：服务总开关关着时它只睡觉（不碰微信 UI），
    # 界面点「开启服务」立即复工，不用再等线程启动。
    # 先起「无障碍闸门保持器」：微信 4.1+ 的自绘 UI 只有闸门开着才暴露
    # mmui 控件树，且这个闸门会被微信自己随时复位，必须常驻保持。
    from scan.extract.wechat_gate import start_keeper

    keeper = start_keeper()
    log.info("微信无障碍闸门保持器已启动", extra={"status": str(keeper.status())})

    _start_wechat_thread(config, service, rules)

    def starter() -> None:
        """界面上点「启用微信采集」时调用。群名是运行时读的，这里只负责起线程。"""
        if not rules.group_name:
            log.warning("还没有采集群，先填群名再启用")
            return
        _start_wechat_thread(config, service, rules)

    _start_auto_thread(config, service)
    # 手机监听跟着程序一起起来：现场只双击程序的场景下，没有它就会
    # 「微信采到了、队列有积压，手机一动不动」。
    from scan import phone_supervisor

    phone_supervisor.start(config)
    serve(config, rules, wechat_starter=starter)
    return 0


def cmd_wechat(_: argparse.Namespace) -> int:
    """采一轮就退出，用于现场排障：能看到「采到几条、入队几条、为什么没采」。"""
    import uiautomation as uia

    from scan.extract.wechat import WeChatCollector, WeChatUnavailable

    config, service = _boot()
    if not config.wechat.group_name:
        print("config.yaml 里 wechat.group_name 还是空的，先填群名")
        return 1
    with uia.UIAutomationInitializerInThread():
        collector = WeChatCollector(config, service)
        try:
            result = collector.poll_once()
        except WeChatUnavailable as exc:
            print("采集失败：", exc)
            return 2
    if result.get("baselined"):
        print("已建立文本消息基线（首轮不采历史文字，文件消息不受影响）。")
    out_of_window = result.get("out_of_window") or 0
    extra = f" | 时间范围外跳过 {out_of_window} 条" if out_of_window else ""
    print(
        f"文字消息新增 {result['new']} 条 | "
        f"表格 {result['files']} 份 | 已导过 {result['skipped']} 份 | "
        f"本地未找到 {result['missing']} 份 | 入队 {result['inserted']} 条{extra}"
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    config, service = _boot()
    from scan.extract import extract_file
    from scan.waybill import rules_signature

    result = extract_file(Path(args.file), config.scan.prefixes, config.scan.lengths)
    print(f"扫描 {result.cells_scanned} 个单元格，识别到 {len(result.waybills)} 个不重复单号")
    for warning in result.warnings[:10]:
        print("  警告:", warning)
    if not result.waybills:
        return 2
    rules = rules_signature(config.scan.prefixes, config.scan.lengths)
    import_ref = f"{result.path.name}:{result.digest[:16]}#{rules}"
    if service.import_seen("excel", import_ref) and not args.force:
        print("这份文件用当前规则已经导过了。要重导加 --force")
        return 0
    enq = service.enqueue(result.waybills, source="excel", source_ref=result.path.name)
    service.record_import("excel", import_ref, result.path.name,
                          enq.total, enq.inserted, enq.duplicated)
    print(f"入队 {enq.inserted} 条，重复 {enq.duplicated} 条")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print("配置错误：", exc)
        return 1
    migrate(config.db_path)
    print(f"配置 OK：{config.server.host}:{config.server.port}")
    print(f"数据库：{config.db_path}")
    print(f"前缀 {config.scan.prefixes}，长度 {config.scan.lengths}，单批上限 {config.scan.batch_max}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        busy = sock.connect_ex(("127.0.0.1", config.server.port)) == 0
    print("端口：" + ("已被占用（如果服务已在跑，属正常）" if busy else "空闲"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run.py", description="运单揽件助手")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="生成 config.yaml")
    p_init.add_argument("path", nargs="?", default=None)
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="启动服务")
    p_serve.set_defaults(func=cmd_serve)

    p_import = sub.add_parser("import", help="导入 Excel/CSV")
    p_import.add_argument("file")
    p_import.add_argument("--force", action="store_true", help="按当前规则强制重导（已导过也重跑）")
    p_import.set_defaults(func=cmd_import)

    p_check = sub.add_parser("check", help="自检")
    p_check.set_defaults(func=cmd_check)

    p_wechat = sub.add_parser("wechat", help="采一轮微信群（排障用）")
    p_wechat.set_defaults(func=cmd_wechat)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
