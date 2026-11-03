"""把 dist/运单自动采集自动揽收助手 打成 Windows 安装包（setup.exe）。

    .venv/Scripts/python.exe tools/make_installer.py
    .venv/Scripts/python.exe tools/make_installer.py --rebuild   # 先重打 dist 再装包

产出：
    dist/运单自动采集自动揽收助手-Setup-<版本>.exe

几条硬规矩（都是踩过的坑）：
  * **绝不把开发机的 config.yaml / data/ 打进安装包**：里面的 token 和 764 条
    待扫队列是调试数据；装到客户机上会直接泄露，而且首次运行不会重新生成 token。
  * 装到 %LOCALAPPDATA%\\Programs 而不是 Program Files：程序要往自己目录写
    data/、cef_cache/，放 Program Files 就得全程管理员权限，adb 拉起也容易被拦。
  * 卸载不删 data/：队列是业务数据，卸载重装不能把没扫完的单号一起带走。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "运单自动采集自动揽收助手"
DIST = ROOT / "dist" / APP_NAME
APP_EXE = f"{APP_NAME}.exe"
ICON = ROOT / "app" / "assets" / "app.ico"
BUILD = ROOT / "build"

# 固定 AppId：升级时能认出是同一个软件，不会在「应用和功能」里出现两条
APP_ID = "8F3A2C1E-6B47-4D9A-9C2E-5D1F7A0B3C64"

# 不打进安装包的运行期文件（相对 dist）
EXCLUDES = (
    "config.yaml",
    "data",
    "inbox",
    "cef_cache",
    "*.log",
    "debug.log",
)


def version() -> str:
    f = ROOT / "VERSION"
    if f.exists():
        text = f.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "1.0.0"


def find_iscc() -> Path:
    """Inno Setup 的编译器，6/7 都认。"""
    for base in (
        Path(r"C:\Program Files\Inno Setup 7"),
        Path(r"C:\Program Files (x86)\Inno Setup 7"),
        Path(r"C:\Program Files\Inno Setup 6"),
        Path(r"C:\Program Files (x86)\Inno Setup 6"),
    ):
        exe = base / "ISCC.exe"
        if exe.exists():
            return exe
    which = shutil.which("ISCC") or shutil.which("iscc")
    if which:
        return Path(which)
    raise SystemExit("没找到 Inno Setup 的 ISCC.exe，先装：winget install JRSoftware.InnoSetup")


def _q(path: Path) -> str:
    """Inno 脚本里的路径用双引号包住，反斜杠原样保留。"""
    return f'"{path}"'


def make_iss(ver: str, iscc: Path) -> Path:
    lang_dir = iscc.parent / "Languages"
    if not (lang_dir / "ChineseSimplified.isl").exists():
        raise SystemExit(f"缺少简体中文语言包：{lang_dir / 'ChineseSimplified.isl'}")

    excludes = ",".join(EXCLUDES)
    lines = f"""
; 由 tools/make_installer.py 生成，改这里没用 —— 改那个脚本
#define MyAppName "{APP_NAME}"
#define MyAppVersion "{ver}"
#define MyAppExe "{APP_EXE}"

[Setup]
AppId={{{{{APP_ID}}}}}
AppName={{#MyAppName}}
AppVersion={{#MyAppVersion}}
AppVerName={{#MyAppName}} {{#MyAppVersion}}
AppPublisher=STO
DefaultDirName={{localappdata}}\\Programs\\{{#MyAppName}}
DefaultGroupName={{#MyAppName}}
DisableProgramGroupPage=yes
; 低权限安装：不弹 UAC，也不弹「为所有用户/仅当前用户」的选择框
PrivilegesRequired=lowest
OutputDir={_q(DIST.parent)}
OutputBaseFilename={APP_NAME}-Setup-{ver}
SetupIconFile={_q(ICON)}
UninstallDisplayIcon={{app}}\\{{#MyAppExe}}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 中英两种语言时默认先弹语言选择框，多一步没意义：固定用第一个（简体中文）
ShowLanguageDialog=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 装/升级时自动关掉正在运行的旧版本，否则 dll 被占用会提示重启
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes
DisableWelcomePage=no
; 版本号写进 exe 属性，方便排查客户装的是哪一版
VersionInfoVersion={ver}
VersionInfoDescription={{#MyAppName}} 安装程序
VersionInfoProductName={{#MyAppName}}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce

[Files]
Source: {_q(DIST / '*')}; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: {_q(excludes)}

[Icons]
Name: "{{group}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExe}}"; WorkingDir: "{{app}}"
Name: "{{autodesktop}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExe}}"; WorkingDir: "{{app}}"; Tasks: desktopicon

[Run]
Filename: "{{app}}\\{{#MyAppExe}}"; Description: "立即启动 {{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清浏览器缓存（几百 MB，删了会自动重建）；data/ 和 config.yaml 一律保留
Type: filesandordirs; Name: "{{app}}\\cef_cache"
""".strip() + "\n"

    out = BUILD / "installer.iss"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Inno 对中文路径敏感，带 BOM 的 UTF-8 最稳
    out.write_text(lines, encoding="utf-8-sig")
    return out


def main() -> int:
    if "--rebuild" in sys.argv:
        print("[1/3] 重新打包绿色目录 ...")
        code = subprocess.call([sys.executable, str(ROOT / "tools" / "package_shell.py")], cwd=ROOT)
        if code != 0:
            raise SystemExit("绿色目录打包失败")

    if not (DIST / APP_EXE).exists():
        raise SystemExit(f"没有可打包的产物：{DIST / APP_EXE}\n先跑 python tools/package_shell.py")

    # 体积别把开发数据算进去，让人一眼看出装出来多大
    files = [p for p in DIST.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files) / 1048576
    print(f"[2/3] 源目录 {len(files)} 个文件 / {total:.0f} MB（已排除 data、config.yaml、cef_cache）")

    ver = version()
    iscc = find_iscc()
    iss = make_iss(ver, iscc)
    print(f"      Inno：{iscc}")
    print(f"      脚本：{iss}")

    target = DIST.parent / f"{APP_NAME}-Setup-{ver}.exe"
    if target.exists():
        target.unlink()

    print("[3/3] 编译安装包（439MB 压缩，通常 2-5 分钟，别关窗口）...")
    t0 = time.time()
    code = subprocess.call([str(iscc), f"/Qp", str(iss)], cwd=ROOT)
    if code != 0 or not target.exists():
        raise SystemExit(f"编译失败（ISCC 退出码 {code}）")

    size = target.stat().st_size / 1048576
    print()
    print(f"完成：{target}")
    print(f"体积：{size:.0f} MB，耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
