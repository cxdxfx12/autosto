"""下载并精简 WebView2 固定版运行时（Fixed Version），放进 vendor/webview2。

    .venv/Scripts/python.exe tools/fetch_webview2.py            # 下载 + 解包 + 精简
    .venv/Scripts/python.exe tools/fetch_webview2.py --keep-all # 不精简语言包

为什么要固定版：
  常青版（Evergreen）要求目标电脑装过 WebView2 运行时。客户机可能没有，
  或者被安全软件管着装不上。固定版是整个 Chromium 内核随包走，
  程序目录里就有 msedgewebview2.exe，完全不依赖系统环境。

产物：vendor/webview2/msedgewebview2.exe 等（约 480MB，打包时原样复制到 dist）
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"
TARGET = VENDOR / "webview2"
PAGE = "https://developer.microsoft.com/en-us/microsoft-edge/webview2/"

# 兜底直链（版本 155.0.4283.62，x64）；正常会从下载页动态解析最新版
FALLBACK = (
    "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/"
    "232cf50b-3980-4531-afbb-779f446d7de1/"
    "Microsoft.WebView2.FixedVersionRuntime.155.0.4283.62.x64.cab"
)

# 保留的界面语言；其余 .pak 每个约 0.8MB，168 个加起来 130MB，没必要
KEEP_LOCALES = {"zh-CN.pak", "en-US.pak", "en-GB.pak"}

# 与本地页面渲染无关的组件：Copilot(AI 侧栏)、Widevine(版权保护 DRM)、安装器
DROP_NAMES = {
    "Copilot.dat",
    "copilotapp.exe",
    "mscopilot.exe",
    "copilot_overlay_images_100_percent.pak",
    "copilot_overlay_images_200_percent.pak",
    "copilot_overlay_unscaled_images.pak",
    "WidevineCdm",
    "Installer",
}


def latest_url() -> str:
    """从官网下载页里解析 x64 固定版 cab 直链，失败就用兜底链接。"""
    try:
        req = urllib.request.Request(PAGE, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"  取下载页失败（{exc}），改用兜底链接")
        return FALLBACK

    html = html.replace("\\u002F", "/").replace("\\/", "/")
    pat = re.compile(
        r"https://msedge\.sf\.dl\.delivery\.mp\.microsoft\.com"
        r"/filestreamingservice/files/[0-9a-f-]{36}"
        r"/Microsoft\.WebView2\.FixedVersionRuntime\.[0-9.]+\.x64\.cab"
    )
    found = pat.findall(html)
    if not found:
        print("  页面里没找到固定版链接，改用兜底链接")
        return FALLBACK
    # 页面按版本倒序，第一个就是最新
    return found[0]


def download(url: str, dest: Path) -> None:
    print(f"下载 {url.rsplit('/', 1)[-1]}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fp.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r  {pct:3d}%  {done / 1048576:.0f}/{total / 1048576:.0f} MB", end="", flush=True)
    print()


def extract(cab: Path, out: Path) -> Path:
    """调系统 expand.exe 解 cab，返回内含的版本目录。"""
    expand = Path(r"C:\Windows\System32\expand.exe")
    if not expand.exists():  # 极简系统上没有，提示手工处理
        raise SystemExit("没找到 expand.exe，请手工把 cab 解到 vendor/webview2")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    print("解包 ...")
    subprocess.run([str(expand), str(cab), "-F:*", str(out)], check=True, stdout=subprocess.DEVNULL)
    dirs = [p for p in out.iterdir() if p.is_dir() and (p / "msedgewebview2.exe").exists()]
    if not dirs:
        raise SystemExit("解包后没找到 msedgewebview2.exe")
    return dirs[0]


def trim(runtime: Path) -> tuple[int, int]:
    """裁掉用不上的语言包/组件，返回（文件数, 省下的 MB）。"""
    before = sum(p.stat().st_size for p in runtime.rglob("*") if p.is_file())

    locates = runtime / "Locales"
    if locates.exists():
        for pak in locates.glob("*.pak"):
            if pak.name not in KEEP_LOCALES:
                pak.unlink()
        for other in locates.glob("*.info"):  # 语言包的校验清单，一并瘦身
            other.unlink(missing_ok=True)

    for name in DROP_NAMES:
        p = runtime / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink(missing_ok=True)

    after = sum(p.stat().st_size for p in runtime.rglob("*") if p.is_file())
    count = sum(1 for p in runtime.rglob("*") if p.is_file())
    return count, int((before - after) / 1048576)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-all", action="store_true", help="不精简，保留全部语言包")
    ap.add_argument("--url", default="", help="手工指定 cab 下载地址")
    args = ap.parse_args()

    VENDOR.mkdir(exist_ok=True)
    cab = VENDOR / "webview2.cab"

    url = args.url or latest_url()
    if not cab.exists() or cab.stat().st_size < 50 * 1048576:
        download(url, cab)
    else:
        print(f"复用已下载的 {cab.name}")

    version_dir = extract(cab, VENDOR / "_wv2_tmp")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.move(str(version_dir), str(TARGET))
    shutil.rmtree(VENDOR / "_wv2_tmp", ignore_errors=True)

    if not args.keep_all:
        count, saved = trim(TARGET)
        print(f"精简完成：保留 {count} 个文件，省下约 {saved} MB")

    size = sum(p.stat().st_size for p in TARGET.rglob("*") if p.is_file()) / 1048576
    ver = next((p.name.replace(".manifest", "") for p in TARGET.glob("*.manifest")), "?")
    print(f"\n运行时版本 {ver}")
    print(f"位置 {TARGET}")
    print(f"体积 {size:.0f} MB")

    cab.unlink(missing_ok=True)  # 解完就没用了，300MB 别留着
    print("下一步：.venv/Scripts/python.exe tools/build_exe.py --with-browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
