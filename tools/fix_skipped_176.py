"""把批次 B20261020-184327-46d4 里被误清成 skipped 的 176 条，补记为 ok。

背景（2026-10-20 18:44）：
  两个操作手机的软件同时抢设备，phone_run 注入成功后点「确定」没生效
  （列表序号最大=0），脚本按设计中止、没有上传。随后有人点了「清除待扫」，
  176 条被标成 skipped（last_error=人工清除待扫）。之后用户人工点了上传，
  申行者里这 176 条实际已揽收成功，但系统不知道。

处理原则：
  · 只改这一批、且只改 last_error 是「人工清除待扫」的 skipped 记录
  · 状态改 ok，result_raw 写明是人工补记，留下审计痕迹
  · 不动任何 ok 记录，不动其他批次

用法：
    .venv/Scripts/python.exe tools/fix_skipped_176.py            # 只预览
    .venv/Scripts/python.exe tools/fix_skipped_176.py --apply    # 真正写入
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone, timedelta

DB = "e:/运单自动采集自动揽收助手/data/scan.db"
BATCH = "B20261020-184327-46d4"
DAY = "2026-10-20"
REASON = "人工清除待扫"
NOTE = "人工补记：18:44 抢设备导致未上传，用户手动在申行者上传成功"


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def main() -> None:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, waybill, status, last_error FROM scan_jobs "
        "WHERE batch_id = ? AND day = ? AND status = 'skipped' AND last_error = ?",
        (BATCH, DAY, REASON),
    ).fetchall()
    print(f"命中 {len(rows)} 条（批次 {BATCH} / 原因「{REASON}」）")
    if rows:
        print("  前 3 条：", [r["waybill"] for r in rows[:3]])

    # 安全校验：确认这批里没有已经 ok 的，避免重复计数
    ok_n = conn.execute(
        "SELECT COUNT(*) FROM scan_jobs WHERE batch_id = ? AND status = 'ok'", (BATCH,)
    ).fetchone()[0]
    print(f"  该批次现有 ok：{ok_n} 条")
    if ok_n:
        print("！该批次已有 ok 记录，为免重复计数，停止")
        conn.close()
        raise SystemExit(1)

    before = conn.execute(
        "SELECT status, COUNT(*) n FROM scan_jobs WHERE day = ? GROUP BY status", (DAY,)
    ).fetchall()
    print(f"  改前统计：{[(r['status'], r['n']) for r in before]}")

    if not apply:
        print("\n预览模式，未写入。确认无误后加 --apply")
        conn.close()
        return

    ts = now_iso()
    cur = conn.execute(
        "UPDATE scan_jobs SET status = 'ok', result_raw = ?, last_error = NULL, "
        "scanned_at = ? WHERE batch_id = ? AND day = ? AND status = 'skipped' "
        "AND last_error = ?",
        (NOTE, ts, BATCH, DAY, REASON),
    )
    conn.commit()
    print(f"\n已更新 {cur.rowcount} 条")

    after = conn.execute(
        "SELECT status, COUNT(*) n FROM scan_jobs WHERE day = ? GROUP BY status", (DAY,)
    ).fetchall()
    print(f"  改后统计：{[(r['status'], r['n']) for r in after]}")
    conn.close()


if __name__ == "__main__":
    main()
