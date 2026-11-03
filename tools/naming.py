"""发单表（Excel）规范命名工具。

为什么要有：
    1.1.4 之前踩过真实事故——业务员在微信群里发的表，因为和本地同名，
    被微信下载时自动加 (1) 后缀（X(2).xlsx → X(2)(1).xlsx），程序按
    文件名去匹配时两层后缀没剥干净，判成「本地找不到」而丢单。
    规范命名（带日期时段）让文件名永不重名，从源头杜绝 (1) 后缀。

用法：
    python tools/naming.py "发单表"                  # 按当前时间生成 MMDD_时段.xlsx
    python tools/naming.py "发单表" --period 0900-1100
    python tools/naming.py --scan inbox/            # 预览每个 xlsx 会被改成什么（干跑）
    python tools/naming.py --scan inbox/ --apply    # 真改名（同名不覆盖，跳过）

安全约定：
    --apply 默认 False；只 --scan 只是读，绝不改文件。
    --apply 遇到目标已存在会跳过并打印告警，不会覆盖。
    路径不在白名单（inbox / tmp / 显式传入的目录）内时拒绝改名，避免误伤项目文件。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phone import ROOT  # 复用项目根，避免写死

PERIOD_BY_HOUR = (
    (5, 11, "上午"),
    (11, 13, "中午"),
    (13, 18, "下午"),
    (18, 23, "晚上"),
    (23, 24, "凌晨"),
    (0, 5, "凌晨"),
)


def period_for_now(now: datetime | None = None) -> str:
    now = now or datetime.now()
    for lo, hi, name in PERIOD_BY_HOUR:
        if lo <= now.hour < hi:
            return name
    return "其他"


def canonical_name(base: str = "", period: str | None = None,
                   now: datetime | None = None) -> str:
    """生成规范文件名：MMDD_时段.xlsx。

    base 只用于保留可读性前缀（可空）；period 缺省按当前时段推断。
    例子：canonical_name("发单表") -> "1103_晚上.xlsx"
    """
    now = now or datetime.now()
    mmdd = now.strftime("%m%d")
    seg = (period or period_for_now(now)).strip() or "其他"
    stem = f"{mmdd}_{seg}"
    if base and base.strip():
        # 去掉可能带来的扩展名/路径，只留前缀
        b = Path(base).stem if "." in base else base
        b = b.strip()
        if b and b not in stem:
            stem = f"{stem}_{b}"
    return f"{stem}.xlsx"


# 允许被改名的目录白名单（相对 ROOT 或绝对）。写错的路径不会动项目文件。
_SCAN_ALLOWED = ("inbox", "tmp", "imports")


def _scan_target(path: Path) -> Path:
    """解析 --scan 目标，越界（不在白名单且非显式传入目录）则拒绝。"""
    p = path.resolve()
    if p.is_dir():
        return p
    # 当成目录名：允许白名单内的相对目录
    if p.name in _SCAN_ALLOWED and not p.exists():
        cand = (ROOT / p.name).resolve()
        if str(cand).startswith(str(ROOT.resolve())):
            return cand
    raise SystemExit(f"--scan 目标只能是白名单目录（{_SCAN_ALLOWED}）或已存在的目录：{path}")


def scan_dir(directory: Path, apply: bool = False) -> list[str]:
    directory = _scan_target(directory)
    sheets = sorted(
        p for p in directory.glob("*")
        if p.suffix.lower() in (".xlsx", ".xlsm", ".csv") and not p.name.startswith("~$")
    )
    if not sheets:
        print(f"[scan] {directory} 下没有 Excel/CSV（或都是临时文件）")
        return []
    results: list[str] = []
    for p in sheets:
        new_name = canonical_name(p.stem)
        if new_name == p.name:
            results.append(f"  跳过（已是规范名）：{p.name}")
            continue
        target = directory / new_name
        if target.exists():
            results.append(f"  跳过（目标已存在，防覆盖）：{p.name} -> {new_name}")
            continue
        results.append(f"  {'改名' if apply else '将改名'}：{p.name} -> {new_name}")
        if apply:
            p.rename(target)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="发单表规范命名工具")
    ap.add_argument("base", nargs="?", default="", help="基础名（可空），仅作可读性前缀")
    ap.add_argument("--period", default=None, help="时段，如 晚上 / 0900-1100；缺省按当前时间推断")
    ap.add_argument("--scan", metavar="DIR", default=None, help="扫描目录，预览/改名其中的 Excel/CSV")
    ap.add_argument("--apply", action="store_true", help="真正改名（默认只干跑预览）")
    args = ap.parse_args()

    if args.scan:
        print(f"[scan] {'改名模式' if args.apply else '预览模式（加 --apply 才真改）'}：{args.scan}")
        for line in scan_dir(Path(args.scan), apply=args.apply):
            print(line)
        return

    print(canonical_name(args.base, period=args.period))


if __name__ == "__main__":
    main()
