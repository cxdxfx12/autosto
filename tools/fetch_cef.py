"""下载 CEF（Chromium Embedded Framework）构建包到 vendor/cef。

    .venv/Scripts/python.exe tools/fetch_cef.py            # 最新 stable，windows64 minimal
    .venv/Scripts/python.exe tools/fetch_cef.py --full     # 标准包（含 cefclient 示例）

CEF 就是「可以把 Chromium 嵌进自己程序」的那套东西，Google 自家的 Chrome、
微软的 Edge 都是 Chromium 内核，CEF 用的就是同一份渲染引擎。
"""

from __future__ import annotations

import argparse
import json
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "cef"
INDEX = "https://cef-builds.spotifycdn.com/index.json"
BASE = "https://cef-builds.spotifycdn.com/"


def pick(kind: str) -> tuple[str, int]:
    """从索引里挑最新 stable 的 windows64 包，返回（文件名, 字节数）。"""
    req = urllib.request.Request(INDEX, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
    versions = data["windows64"]["versions"]
    for item in versions:  # 索引按新到旧排
        if item.get("channel") != "stable":
            continue
        for f in item.get("files", []):
            if f.get("type") == kind:
                return f["name"], int(f.get("size") or 0)
    raise SystemExit(f"索引里没找到 {kind} 类型的 windows64 包")


def download(name: str, size: int, dest: Path) -> None:
    url = BASE + urllib.parse.quote(name)
    print(f"下载 {name}（{size / 1048576:.0f} MB）")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fp:
        done = 0
        last = 0.0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fp.write(chunk)
            done += len(chunk)
            now = time.time()
            if now - last > 2:
                last = now
                pct = done * 100 // max(size, 1)
                print(f"\r  {pct:3d}%  {done / 1048576:.0f}/{size / 1048576:.0f} MB", end="", flush=True)
    print()


def extract(archive: Path, dest: Path) -> Path:
    print("解包 ...（bz2 解压比较慢，几分钟正常）")
    t0 = time.time()
    with tarfile.open(archive, "r:bz2") as tf:
        tf.extractall(dest, filter="data")
    dirs = [p for p in dest.iterdir() if p.is_dir() and (p / "include" / "cef_app.h").exists()]
    if not dirs:
        raise SystemExit("解包后没找到 include/cef_app.h")
    print(f"解包完成 {time.time() - t0:.0f}s")
    return dirs[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="下载标准包而不是 minimal")
    args = ap.parse_args()

    kind = "standard" if args.full else "minimal"
    VENDOR.mkdir(parents=True, exist_ok=True)
    name, size = pick(kind)
    archive = VENDOR / name

    if not archive.exists() or archive.stat().st_size < size * 0.95:
        download(name, size, archive)
    else:
        print(f"复用已下载的 {name}")

    root = extract(archive, VENDOR)
    archive.unlink(missing_ok=True)
    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) / 1048576
    print(f"\nCEF 就绪：{root}")
    print(f"体积 {total:.0f} MB")
    print("下一步：.venv/Scripts/python.exe tools/build_shell.py")
    return 0


if __name__ == "__main__":
    import urllib.parse

    raise SystemExit(main())
