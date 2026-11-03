"""后台核心服务（无窗口）。由 C++ 外壳静默拉起，也可以单独跑排障。

    miao-core.exe          # HTTP 服务 + 微信采集 + 自动开批，全部在这里

为什么要单独一个 exe：
  C++ 外壳只负责窗口和浏览器内核；业务逻辑（微信 UIA 采集、Excel 解析、SQLite）
  仍然用 Python —— 这部分重写不值得，也不稳。外壳启动时把它当子进程拉起来，
  等 8766 端口通了再打开界面。
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


def _bases() -> tuple[Path, Path]:
    """（数据目录, 只读资源目录）。

    冻结后本 exe 在 core\\ 子目录里，数据（config.yaml / data / inbox）
    必须放在**程序根**（core 的上一级），和 C++ 外壳的 ExeDir() 对齐。
    """
    if getattr(sys, "frozen", False):
        core_dir = Path(sys.executable).resolve().parent
        base = core_dir.parent if core_dir.name.lower() == "core" else core_dir
        return base, Path(getattr(sys, "_MEIPASS", base))
    base = Path(__file__).resolve().parent
    return base, base


def _tools_dir(res: Path, base: Path) -> Path | None:
    for cand in (res / "tools", base / "tools", Path(__file__).resolve().parent / "tools"):
        if cand.is_dir():
            return cand
    return None


def _ensure_stdio() -> None:
    """--windowed 打包后没有控制台，sys.stdout/stderr 是 None，print 会直接抛。

    tools 下那几个脚本大量用 print 汇报进度（日志也靠它），所以子进程入口
    先兜一层 devnull —— 有重定向（supervisor 就是重定向到文件）时仍然照常写。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — 进程级，常驻
    if sys.stderr is None:
        sys.stderr = sys.stdout


def _run_tool(mode: str, rest: list[str]) -> int:
    """内部子进程入口：程序自己拉起手机监听 / 跑一轮揽收。

    为什么不放进独立 exe：tools 下这几个脚本是现场排障的主力，
    改动频繁；冻结成 exe 每改一次都要重新打包。这里用「同一个 exe + 参数」
    的方式复用它们，既保留了**独立进程**的隔离性（adb/UIA 崩了不带走后端），
    又不用往安装包里塞 Python 解释器。

    环境变量（MIAO_ROOT / PATH / STO_SERVER_TOKEN）由 scan.phone_supervisor
    在启动子进程时注入，这里只负责补齐，保证手动执行也能跑。
    """
    base, res = _bases()
    _ensure_stdio()
    os.environ.setdefault("MIAO_ROOT", str(base))
    tools = _tools_dir(res, base)
    if tools is None:
        print(f"找不到 tools 目录（找过 {res / 'tools'}）", file=sys.stderr)
        return 2
    sys.path.insert(0, str(tools))
    adb_dir = base / "vendor" / "platform-tools"
    if (adb_dir / "adb.exe").exists():
        os.environ["PATH"] = str(adb_dir) + os.pathsep + os.environ.get("PATH", "")

    if mode == "--phone-watch":
        import phone_watch  # noqa: PLC0415 — 延迟 import，正常运行不该加载 adb 依赖

        sys.argv = ["phone-watch", *rest]
        return int(phone_watch.main() or 0)

    if mode == "--phone-run":
        import phone_arbiter  # noqa: PLC0415
        import phone_run  # noqa: PLC0415

        sys.argv = ["phone-run", *rest]
        try:
            phone_run.main()
        finally:
            # 让路必须在任何退出路径上解除（main 里遍地 SystemExit）
            phone_arbiter.release_peer()
        return 0

    print(f"未知的子进程入口：{mode}", file=sys.stderr)
    return 2


def _patch(base: Path, res: Path) -> None:
    import scan.config as cfgmod
    import scan.rules as rulesmod
    import scan.server as srvmod

    cfgmod.ROOT = base
    rulesmod.CONFIG_PATH = base / "config.yaml"
    srvmod.WEB_DIR = res / "app" / "web"


def main() -> int:
    argv = sys.argv[1:]
    # 内部子进程入口先分流：这两个分支不需要配置/数据库，也不该再起监听（防套娃）
    if argv and argv[0] in ("--phone-watch", "--phone-run"):
        return _run_tool(argv[0], argv[1:])

    base, res = _bases()
    _patch(base, res)

    import run

    cfg = base / "config.yaml"
    if not cfg.exists():
        token = secrets.token_urlsafe(24)
        (base / "data").mkdir(parents=True, exist_ok=True)
        cfg.write_text(run.CONFIG_TEMPLATE.replace("{token}", token), encoding="utf-8")
        # 交给外壳去弹窗，这里只落文件（后台进程没有窗口）
        (base / "data" / "first-run-token.txt").write_text(token, encoding="utf-8")

    class _Args:
        pass

    return run.cmd_serve(_Args())


if __name__ == "__main__":
    raise SystemExit(main())
