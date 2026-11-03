"""编译 C++ 外壳（Win32 + CEF），并把它运行需要的 Chromium 运行时拷到旁边。

    .venv/Scripts/python.exe tools/build_shell.py            # 编译
    .venv/Scripts/python.exe tools/build_shell.py --clean    # 重编 wrapper

不依赖 CMake：直接用本机 MSVC 的 cl.exe / lib.exe / link.exe。
wrapper（libcef_dll，213 个 .cc）编译一次后缓存成 build/shell/libcef_dll_wrapper.lib，
后续只重编 main.cpp，几秒出结果。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHELL_SRC = ROOT / "shell"
OUT = ROOT / "build" / "shell"
CEF_ROOT = ROOT / "vendor" / "cef"

MSVC_HOME = Path(r"D:\tools\VSBuildTools\VC\Tools\MSVC")
KITS = Path(r"C:\Program Files (x86)\Windows Kits\10")


def _pick_msvc() -> Path:
    """取版本号最大的那个 MSVC（客户机上装的位置/版本可能不同）。"""
    cands = sorted([p for p in MSVC_HOME.glob("14.*") if (p / "bin" / "Hostx64" / "x64" / "cl.exe").exists()])
    if not cands:
        raise SystemExit(f"没找到 MSVC 编译器，检查 {MSVC_HOME}")
    return cands[-1]


def _pick_sdk() -> str:
    vers = sorted([p.name for p in (KITS / "Include").glob("10.0.*") if (p / "um" / "windows.h").exists()])
    if not vers:
        raise SystemExit(f"没找到 Windows SDK，检查 {KITS / 'Include'}")
    return vers[-1]


MSVC = _pick_msvc()
SDK_VER = _pick_sdk()

APP_EXE = "运单自动采集自动揽收助手.exe"


def find_cef() -> Path:
    """vendor/cef 下面那个带 include/ 和 Release/ 的目录。"""
    for p in CEF_ROOT.glob("cef_binary_*"):
        if (p / "include" / "cef_app.h").exists() and (p / "Release" / "libcef.lib").exists():
            return p
    raise SystemExit("没找到 CEF，先跑：python tools/fetch_cef.py")


def env_for_msvc() -> dict:
    """手工拼 INCLUDE/LIB/PATH —— 比 call vcvars 稳，不受 cmd 限制。"""
    inc = [
        MSVC / "include",
        KITS / "Include" / SDK_VER / "ucrt",
        KITS / "Include" / SDK_VER / "um",
        KITS / "Include" / SDK_VER / "shared",
        KITS / "Include" / SDK_VER / "winrt",
    ]
    lib = [
        MSVC / "lib" / "x64",
        KITS / "Lib" / SDK_VER / "ucrt" / "x64",
        KITS / "Lib" / SDK_VER / "um" / "x64",
    ]
    env = os.environ.copy()
    env["INCLUDE"] = ";".join(str(p) for p in inc)
    env["LIB"] = ";".join(str(p) for p in lib)
    env["PATH"] = ";".join([
        str(MSVC / "bin" / "Hostx64" / "x64"),
        str(KITS / "bin" / SDK_VER / "x64"),
        env.get("PATH", ""),
    ])
    return env


FLAGS = [
    # CEF 152 的头文件用了 std::convertible_to，必须 C++20
    "/nologo", "/c", "/O2", "/MD", "/EHsc", "/std:c++20", "/bigobj",
    "/DNDEBUG", "/D_WINDOWS", "/DUNICODE", "/D_UNICODE", "/DNOMINMAX",
    "/D_CRT_SECURE_NO_WARNINGS", "/D_SCL_SECURE_NO_WARNINGS",
    # 源码是 UTF-8（里面有中文），不加这个 MSVC 会按 GBK 解析，中文字面量直接报错
    "/utf-8",
    "/W3",
]


def run(cmd: list[str], env: dict, cwd: Path | None = None, quiet: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, env=env, cwd=str(cwd or ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0 and not quiet:
        print(proc.stdout.decode("utf-8", "replace")[-4000:])
    return proc


def build_wrapper(cef: Path, env: dict, cl: str) -> Path:
    """编译 libcef_dll（C++ 封装层）成静态库，缓存复用。"""
    lib_path = OUT / "libcef_dll_wrapper.lib"
    stamp = OUT / f".wrapper_{cef.name}.stamp"
    if lib_path.exists() and stamp.exists():
        print(f"复用已编译的 {lib_path.name}")
        return lib_path

    sources = [
        p for p in (cef / "libcef_dll").rglob("*.cc")
        if "\\test\\" not in str(p) and "/test/" not in str(p)
    ]
    print(f"编译 CEF wrapper：{len(sources)} 个源文件 ...")
    obj_dir = OUT / "wrapper_obj"
    if obj_dir.exists():
        shutil.rmtree(obj_dir)
    obj_dir.mkdir(parents=True)

    extra = ["/I", str(cef), "/I", str(cef / "include"),
             # wrapper 侧的源文件必须带这个宏，否则一律 #error 拒绝编译
             "/DWRAPPING_CEF_SHARED"]

    def one(src: Path) -> tuple[Path, bool]:
        obj = obj_dir / (src.stem + ".obj")
        # 重名（cpptoc/ctocpp 里可能有同名文件）时加目录前缀
        if obj.exists():
            obj = obj_dir / (src.parent.name + "_" + src.stem + ".obj")
        proc = run([cl] + FLAGS + extra + [f"/Fo{obj}", str(src)], env)
        return obj, proc.returncode == 0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) // 2)) as pool:
        results = list(pool.map(one, sources))
    failed = [str(p) for p, ok in results if not ok]
    if failed:
        print(f"有 {len(failed)} 个文件编译失败，例如：{failed[:3]}")
        raise SystemExit(1)
    objs = [str(p) for p, _ in results]

    (OUT / "wrapper_objs.rsp").write_text("\n".join(f'"{o}"' for o in objs), encoding="utf-8")
    proc = run([str(MSVC / "bin" / "Hostx64" / "x64" / "lib.exe"), "/nologo",
                f"/OUT:{lib_path}", f"@{OUT / 'wrapper_objs.rsp'}"], env)
    if proc.returncode != 0:
        raise SystemExit("lib.exe 打包 wrapper 失败")
    stamp.write_text("ok", encoding="utf-8")
    print(f"wrapper 编译完成（{time.time() - t0:.0f}s）")
    return lib_path


def build_exe(cef: Path, env: dict) -> Path:
    cl = str(MSVC / "bin" / "Hostx64" / "x64" / "cl.exe")
    link = str(MSVC / "bin" / "Hostx64" / "x64" / "link.exe")
    rc_tool = str(KITS / "bin" / SDK_VER / "x64" / "rc.exe")

    wrapper = build_wrapper(cef, env, cl)

    # 图标：rc.exe 按当前目录找文件，先拷到 build 目录
    icon = OUT / "app.ico"
    shutil.copy2(ROOT / "app" / "assets" / "app.ico", icon)

    print("编译 main.cpp ...")
    main_obj = OUT / "main.obj"
    proc = run([cl] + FLAGS + ["/I", str(cef), "/I", str(cef / "include"), "/I", str(SHELL_SRC),
                               f"/Fo{main_obj}", str(SHELL_SRC / "main.cpp")], env)
    if proc.returncode != 0:
        raise SystemExit("main.cpp 编译失败")

    print("编译资源（图标/版本信息）...")
    res_obj = OUT / "app.res"
    proc = run([rc_tool, "/nologo", f"/fo{res_obj}", str(SHELL_SRC / "app.rc")], env, cwd=OUT)
    if proc.returncode != 0:
        raise SystemExit("资源编译失败")

    print("链接 ...")
    exe = OUT / APP_EXE
    cmd = [
        link, "/nologo", "/SUBSYSTEM:WINDOWS", "/OPT:REF", "/OPT:ICF",
        f"/OUT:{exe}",
        str(main_obj), str(res_obj), str(wrapper),
        str(cef / "Release" / "libcef.lib"),
        "user32.lib", "gdi32.lib", "shell32.lib", "advapi32.lib", "ole32.lib",
        "oleaut32.lib", "uuid.lib", "shlwapi.lib", "comdlg32.lib", "winspool.lib",
        "comctl32.lib", "version.lib", "ws2_32.lib", "dwmapi.lib",
    ]
    proc = run(cmd, env)
    if proc.returncode != 0:
        raise SystemExit("链接失败")
    return exe


def copy_runtime(cef: Path) -> None:
    """把 Chromium 运行时摆到 exe 旁边（CEF 按 exe 目录找）。"""
    print("拷贝 Chromium 运行时 ...")
    for src in (cef / "Release").iterdir():
        if src.is_file():
            shutil.copy2(src, OUT / src.name)
    res = cef / "Resources"
    for src in res.iterdir():
        dst = OUT / src.name
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    # locales 只要中日韩英，其余 100 多个 .pak 白占 30MB
    locales = OUT / "locales"
    keep = {"zh-CN.pak", "zh-TW.pak", "en-US.pak", "en-GB.pak"}
    if locales.exists():
        for pak in locales.glob("*.pak"):
            if pak.name not in keep:
                pak.unlink()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="丢掉缓存的 wrapper 重新编译")
    args = ap.parse_args()

    if not MSVC.exists():
        raise SystemExit(f"没找到 MSVC：{MSVC}")
    if not (KITS / "Include" / SDK_VER).exists():
        raise SystemExit(f"没找到 Windows SDK {SDK_VER}")

    cef = find_cef()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for p in OUT.glob("*.lib"):
            p.unlink()
        for p in OUT.glob(".wrapper_*.stamp"):
            p.unlink()

    env = env_for_msvc()
    t0 = time.time()
    exe = build_exe(cef, env)
    copy_runtime(cef)

    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file()) / 1048576
    print()
    print(f"外壳编译完成（{time.time() - t0:.0f}s）")
    print(f"  {exe}")
    print(f"  整目录 {size:.0f} MB（其中 libcef.dll 占大头）")
    print("下一步：python tools/package_shell.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
