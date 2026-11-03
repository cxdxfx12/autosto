"""微信群采集（PC 微信 4.x，只读 UIA）。

实测依据（ddwx-bridge 2026-10-16 抓取的控件树 + wechat4.py 生产验证）：
    消息列表   mmui::RecyclerListView
      ├ mmui::ChatTextItemView    name = 正文
      ├ mmui::ChatItemView        name = 时间戳（"12:25" / "昨天 19:12" / "星期一"）
      └ mmui::ChatBubbleItemView  name = "文件\\nxxx.xlsx\\n35.7K\\n微信电脑版"

三条约束：
1. **只读**。不点、不切群、不输入——切换会话会抢用户的鼠标。
2. **群名护栏**。标题不是配置的群，一条都不采（防串群）。
3. **基线**。首次启动只记录当前屏，不把历史消息灌进队列；之后只采新增。
   基线存 runtime 表，重启不会把已经采过的消息再采一遍。

已知故障态：微信某些启动/托盘往返后主窗口会变成 `Qt51514QWindowIcon` 自绘壳，
UIA 子树为空。此时完全退出微信再打开即可，代码不用改。
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import hashlib
import json
import queue
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Protocol

from ..log import get_logger

log = get_logger("scan.wechat")

TEXT_ITEM = "mmui::ChatTextItemView"
TIME_ITEM = "mmui::ChatItemView"
BUBBLE_ITEM = "mmui::ChatBubbleItemView"

# 气泡里带表格时 name 的形态："文件\n10.19上午需揽收.xlsx\n40.0K\n微信电脑版"
_FILE_TYPES = {"文件", "[文件]", "聊天记录", "[聊天记录]"}
_TABLE_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".csv")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMG]?)B?$", re.I)
# 微信重名副本：丹鸟(17).xlsx / 10.19上午需揽收(1).xlsx
_COPY_NO_RE = re.compile(r"\(\d+\)$")
IMPORT_KIND = "wechat_file"

# "12:25" / "昨天 19:12" / "星期一" / "10-16" / "2026年10月16日 12:25"
_TIME_PATTERNS = (
    re.compile(r"^\d{1,2}:\d{2}$"),
    re.compile(r"^(昨天|前天|今天|星期[一二三四五六日天])\s*\d{0,2}:?\d{0,2}$"),
    re.compile(r"^\d{1,2}[-/月]\d{1,2}日?(\s+\d{1,2}:\d{2})?$"),
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日(\s+\d{1,2}:\d{2})?$"),
)


class WeChatUnavailable(RuntimeError):
    """读不到微信窗口 / 读不到控件树，附带给人看的处理建议。"""


# ---------- 自绘壳自愈 ----------

# 微信 4.x 偶发把主窗口渲染成 Qt 自绘壳（UIA 树为空）。
# 实测（2026-10-28）：**重启并不保证恢复** —— 若微信自动登录跳过登录页，
# UIA 仍是空壳；必须走完「登录确认 → 点进入微信 → 主界面」这一趟才行。
# 所以自愈只能做到「杀掉重启 + 提示人点一下」，最后那步必须人来。
_SHELL_HEAL_AT = 0.0            # 上次自愈时间（模块级，跨采集轮次共享）
_SHELL_HEAL_COOLDOWN = 600.0    # 冷却 10 分钟：重启也没好/反复退化时不再连杀

# 微信主窗口 / 登录页的类名（4.x 实测）
MAIN_WINDOW_CLASS = "mmui::MainWindow"
SHELL_CLASS = "Qt51514QWindowIcon"
LOGIN_CLASS = "mmui::LoginWindow"


def _weixin_exe() -> Path | None:
    """从注册表找 Weixin.exe。HKCU 优先，找不到返回 None（降级为只杀不启）。"""
    try:
        import winreg

        for root, sub in (
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Weixin"),
        ):
            try:
                with winreg.OpenKey(root, sub) as key:
                    value, _ = winreg.QueryValueEx(key, "InstallPath")
                exe = Path(value) / "Weixin.exe"
                if exe.exists():
                    return exe
            except OSError:
                continue
    except Exception:  # noqa: BLE001 —— 注册表读不到不影响主流程
        pass
    return None


def _weixin_pids() -> list[int]:
    """列出所有 Weixin.exe 的 pid（含子进程）。

    只杀这个进程树，不用 `taskkill /IM Weixin.exe` —— 那会把用户另外登录的
    微信号一起打死。
    """
    pids: list[int] = []
    try:
        import subprocess

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace",
            creationflags=flags, check=False, timeout=15,
        ).stdout or ""
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
    except Exception:  # noqa: BLE001
        pass
    return pids


def _restart_weixin() -> str:
    """杀掉微信并重新拉起。返回给人看的处理说明（作为异常文案抛出）。

    注意：这里跑在采集线程里，会阻塞 5~10 秒。只在真的检测到自绘壳时调用，
    且有 10 分钟冷却，不会把正常采集拖慢。
    """
    import subprocess

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    pids = _weixin_pids()
    if not pids:
        return (
            "微信主窗口是自绘壳且微信进程不在，请手动打开微信并点「进入微信」后重试。"
        )
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, creationflags=flags, check=False, timeout=15,
        )
    time.sleep(3)

    exe = _weixin_exe()
    if exe is None:
        return "微信已退出，请手动重新打开微信并点「进入微信」。"
    try:
        # DETACHED_PROCESS：微信不挂在采集进程名下，采集进程退出也不带走它
        detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(exe)], creationflags=detached, close_fds=True)
    except OSError:
        return "微信已退出但自动启动失败，请手动打开微信并点「进入微信」。"
    # 等它把窗口拉出来（实测冷启动 10~20s），别让人以为没反应
    deadline = time.time() + 20
    while time.time() < deadline:
        if _weixin_pids():
            break
        time.sleep(1.0)
    return (
        "微信退化成了 Qt 自绘壳，已自动重启微信。"
        "请在微信窗口点「进入微信」（登录态还在，不用扫码），再点一次立即抓取。"
    )


def _shell_message(cls: str) -> str:
    """自绘壳的处理文案；冷却窗口外顺手触发一次自愈。"""
    global _SHELL_HEAL_AT
    now = time.time()
    if _SHELL_HEAL_AT and now - _SHELL_HEAL_AT < _SHELL_HEAL_COOLDOWN:
        left = int((_SHELL_HEAL_COOLDOWN - (now - _SHELL_HEAL_AT)) / 60) + 1
        return (
            f"微信主窗口退化成了 Qt 自绘壳（class={cls}），控件树是空的。"
            f"已自动重启过微信，{left} 分钟内不再重复重启。"
            "请点一下微信窗口的「进入微信」，然后重试立即抓取。"
        )
    _SHELL_HEAL_AT = now
    return _restart_weixin()


def _measure_uia_tree(uia, max_depth: int = 3) -> int:
    """数一下微信自绘壳窗口下的 UIA 节点数，只用于日志排障。

    正常主窗口几百个节点；自绘壳恒为 3（Qt壳 + Weixin子窗 + MMUIRenderSubWindowHW）。
    """
    try:
        w = uia.WindowControl(searchDepth=1, RegexName=".*微信.*", ClassName=SHELL_CLASS)
        if not w.Exists(0.5, 1):
            return 0
        count = 0
        for _ctrl, _depth in uia.WalkControl(w, includeTop=True, maxDepth=max_depth):
            count += 1
        return count
    except Exception:  # noqa: BLE001 —— 纯诊断，失败就当 0
        return 0


def _login_window_exists(uia) -> bool:
    """微信是否停在「进入微信」登录确认页（class=mmui::LoginWindow）。

    这个状态 UIA 树是正常的，重启解决不了，只能等人点 —— 必须和自绘壳区分开。
    """
    try:
        return uia.WindowControl(searchDepth=1, ClassName=LOGIN_CLASS).Exists(1, 1)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class Message:
    kind: str      # text / file
    text: str
    fingerprint: str
    # 这条消息属于哪一天（由时间分隔条推出来）。屏幕最上面没有分隔条时为 None。
    when: datetime | None = None


_CN_DAYS = "一二三四五六日"


def _parse_day(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_separator(text: str, now: datetime | None = None) -> datetime | None:
    """时间分隔条 → 具体时间。解不出来返回 None（宁可不采，也别猜错日期）。

    微信的几种形态：
        09:35              今天
        昨天 19:12 / 前天 08:00
        星期二             最近的那个星期二
        10月16日 / 10-16   今年的这一天（比今天晚就当成去年）
        2026年10月16日 12:25
    """
    now = now or datetime.now()
    value = (text or "").strip()
    if not value:
        return None

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if m:
        return now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)

    m = re.fullmatch(r"(昨天|前天)\s*(\d{1,2}):(\d{2})", value)
    if m:
        days = 1 if m[1] == "昨天" else 2
        base = now - timedelta(days=days)
        return base.replace(hour=int(m[2]), minute=int(m[3]), second=0, microsecond=0)

    m = re.fullmatch(r"星期([一二三四五六日天])", value)
    if m:
        idx = _CN_DAYS.index("日" if m[1] == "天" else m[1])
        delta = (now.weekday() - idx) % 7 or 7  # 不含今天：当天会显示成时间
        return (now - timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)

    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?", value)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0))

    m = re.fullmatch(r"(\d{1,2})[-/月](\d{1,2})日?(?:\s*(\d{1,2}):(\d{2}))?", value)
    if m:
        try:
            dt = datetime(int(now.year), int(m[1]), int(m[2]), int(m[3] or 0), int(m[4] or 0))
        except ValueError:
            return None
        if dt.date() > now.date():
            dt = dt.replace(year=now.year - 1)
        return dt
    return None


def is_time_separator(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    return any(p.match(value) for p in _TIME_PATTERNS)


def fingerprint(kind: str, text: str) -> str:
    return hashlib.sha1(f"{kind}\x00{text}".encode("utf-8")).hexdigest()[:16]


def parse_items(
    items: Iterable[tuple[str, str]], now: datetime | None = None
) -> list[Message]:
    """把 (className, name) 列表变成消息列表。

    纯函数：不依赖 UIA，方便用真实抓包数据做回归测试。
    时间分隔条本身不算消息（否则 `12:25` 会被当成正文），
    但要拿它推「下面这批消息是哪一天的」，供「只采当天」过滤使用。
    """
    now = now or datetime.now()
    out: list[Message] = []
    current: datetime | None = None
    for class_name, name in items:
        text = (name or "").strip()
        if not text:
            continue
        if class_name == TIME_ITEM or is_time_separator(text):
            current = parse_separator(text, now)
            continue
        if class_name == TEXT_ITEM:
            kind = "text"
        elif class_name == BUBBLE_ITEM:
            kind = "file"
        else:
            continue
        out.append(
            Message(kind=kind, text=text, fingerprint=fingerprint(kind, text), when=current)
        )
    return out


def group_matches(title: str, expected: str) -> bool:
    """群名比对：微信标题可能带 " (13)" 这类人数后缀，也可能有不可见空白。"""
    if not expected:
        return False

    def norm(s: str) -> str:
        s = re.sub(r"\s+", "", s)
        s = re.sub(r"[(（]\d+[)）]$", "", s)
        return s

    return norm(title) == norm(expected)


# ---------- 文件消息 ----------


@dataclass(frozen=True)
class FileRef:
    """群消息里那个表格的「身份证」：文件名 + 显示大小。

    大小是必需的。同一个群每天发 `丹鸟.xlsx`，微信存成本地副本时依次改名
    `丹鸟(15)(16)(17)` —— 光靠文件名根本无法确定消息指的是哪一份。
    但消息气泡里带着 "10.8K"，本机文件 11051 字节正好是 10.8K，
    而 10 月 2 日那份旧 `丹鸟.xlsx` 是 10.5K，一比对就排除了。
    """

    name: str
    kb: float | None


def parse_file_bubble(text: str) -> FileRef | None:
    """从气泡正文里取文件名和大小；不是表格类文件返回 None。"""
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    if len(lines) < 2 or lines[0] not in _FILE_TYPES:
        return None
    for i, line in enumerate(lines[1:], start=1):
        if not line.lower().endswith(_TABLE_SUFFIXES):
            continue
        kb: float | None = None
        for nxt in lines[i + 1:]:
            m = _SIZE_RE.match(nxt)
            if not m:
                continue
            value = float(m.group(1))
            unit = (m.group(2) or "").upper()
            kb = value * {"": 1 / 1024, "K": 1, "M": 1024, "G": 1024 * 1024}[unit]
            break
        return FileRef(name=line, kb=kb)
    return None


def base_stem(stem: str) -> str:
    """`丹鸟(17)` → `丹鸟`，用来把重名副本归到同一个基名。

    微信落盘遇重名会自行加 (1)(2)…；如果消息里的文件名**本身就带 (N)**
    （如群里发「新建 XLSX 工作表 (2).xlsx」），磁盘上会变成
    「新建 XLSX 工作表 (2)(1).xlsx」——只剥一层会让两边不对称：
    磁盘剥成「…(2)」、消息剥成「…」，永远判「本地没找到」，
    文件明明在硬盘上却被静默跳过（2026-10-29 因此丢 98 单）。
    所以反复剥到不再变化为止。
    """
    cur = (stem or "").strip()
    while True:
        nxt = _COPY_NO_RE.sub("", cur).strip()
        if nxt == cur:
            return cur
        cur = nxt


def default_file_root() -> Path | None:
    """微信 4.x 与 3.x 的默认接收目录。"""
    home = Path.home()
    for cand in (
        home / "Documents" / "xwechat_files",
        home / "Documents" / "WeChat Files",
    ):
        if cand.is_dir():
            return cand
    return None


class WeChatFileLocator:
    """把群消息里的文件引用，映射到微信已经下载到本机的那份文件。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_file_root()

    def _candidates(self, suffix: str) -> Iterable[Path]:
        if not self.root or not self.root.is_dir():
            return []
        # 标准结构：<root>/<wxid>/msg/file/<YYYY-MM>/<name>
        dirs = [d for d in self.root.glob("*/msg/file") if d.is_dir()]
        if not dirs:
            dirs = [self.root]
        out: list[Path] = []
        for base in dirs:
            out.extend(base.glob(f"*/*{suffix}"))
            out.extend(base.glob(f"*{suffix}"))
        return out

    def find(self, ref: FileRef) -> Path | None:
        """基名相同 + 大小吻合，取最新的一份。找不到大小吻合的就返回 None。"""
        target = base_stem(Path(ref.name).stem)
        suffix = Path(ref.name).suffix.lower()
        best: tuple[float, Path] | None = None
        for path in self._candidates(suffix):
            if base_stem(path.stem) != target:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if ref.kb is not None and abs(stat.st_size / 1024 - ref.kb) > 0.15:
                continue
            if best is None or stat.st_mtime > best[0]:
                best = (stat.st_mtime, path)
        return best[1] if best else None


class _Control(Protocol):
    ClassName: str
    Name: str

    def GetChildren(self) -> list["_Control"]: ...
    def Control(self, **kwargs: object) -> "_Control": ...
    def Exists(self, *args: object) -> bool: ...


def _first_name(root: "_Control", max_depth: int = 8) -> str:
    """广度优先取子树里第一个非空 Name。

    广度优先是关键：标题栏外层节点都是无名的容器，群名在较浅的
    XHBoxView 上；再往深处才是「更多」「成员数」之类的按钮。
    """
    queue: list[tuple[object, int]] = [(root, 0)]
    while queue:
        node, depth = queue.pop(0)
        if depth > 0:
            name = (getattr(node, "Name", "") or "").strip()
            if name:
                return name
        if depth >= max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            continue
        queue.extend((child, depth + 1) for child in children)
    return ""


class WeChatReader:
    """只读打开当前会话。用完即释放，不常驻。"""

    WINDOW_CLASS = MAIN_WINDOW_CLASS   # 用模块级常量，避免两处定义漂移
    MESSAGE_LIST = "mmui::RecyclerListView"
    TITLE_BAR = "mmui::ChatTitleBarChatRoomView"
    SEARCH_DEPTH = 30

    def __init__(self) -> None:
        self._uia = None
        self._wnd = None

    def _load(self):
        if self._uia is None:
            try:
                import uiautomation as uia
            except ImportError as exc:  # pragma: no cover
                raise WeChatUnavailable("缺少依赖：pip install uiautomation comtypes") from exc
            uia.SetGlobalSearchTimeout(3)
            self._uia = uia
        return self._uia

    def open(self) -> None:
        uia = self._load()
        wnd = uia.WindowControl(searchDepth=1, ClassName=self.WINDOW_CLASS)
        # 第一次只等 2s。自绘壳/登录页是常驻状态，等再久也没用；而主窗口正常时
        # UIA 通常 1s 内就返回，没必要每次都耗满 2 秒。
        if not wnd.Exists(2, 1):
            measured = _measure_uia_tree(uia)
            # 兜底一：登录确认页。UIA 树是好的，重启没用，只能提示人点。
            if _login_window_exists(uia):
                raise WeChatUnavailable(
                    "微信停在登录确认页：请在微信窗口点「进入微信」后重试。"
                )
            # 兜底二：Qt 自绘壳（UIA 空树）。注意排除自己家的桥接程序 ——
            # 「喵喵钉钉-微信消息转发桥」标题也带微信俩字，类名 TkTopLevel，
            # 之前就把它误判成「微信退化成自绘壳」，白白吓人。
            shell = uia.WindowControl(
                searchDepth=1, RegexName=".*微信.*", ClassName=SHELL_CLASS
            )
            if shell.Exists(1, 1):
                log.warning(
                    "微信主窗口是自绘壳，开始自愈",
                    extra={"status": f"节点数={measured}"},
                )
                raise WeChatUnavailable(_shell_message(shell.ClassName))
            raise WeChatUnavailable("找不到微信主窗口，请先打开微信并进入目标群聊。")
        self._wnd = wnd

    def title(self) -> str:
        """回读群名。

        实测（2026-10-19）：群名不在标题栏的直接子节点上，而是埋在
        `ChatTitleBarChatRoomView → XVBoxView → XHBoxView(name=群名)` 里。
        旧实现只取直接子节点的 Name，读出来恒为空串，导致群名护栏无法生效
        （所有群都会被判成「不是配置的群」而拒采）。
        因此改为：先按 ddwx 生产验证过的 XTextView 取，取不到再做广度遍历取
        第一个非空 Name —— 广度优先保证拿到的是最外层那个，不会误取深层按钮。
        """
        assert self._wnd is not None
        try:
            bar = self._wnd.Control(ClassName=self.TITLE_BAR, searchDepth=self.SEARCH_DEPTH)
        except Exception:
            return ""
        try:
            tv = bar.TextControl(ClassName="mmui::XTextView", searchDepth=10)
            if tv.Exists(0.3) and (tv.Name or "").strip():
                return (tv.Name or "").strip()
        except Exception:
            pass
        return _first_name(bar)

    def read_items(self) -> list[tuple[str, str]]:
        assert self._wnd is not None
        try:
            msg_list = self._wnd.Control(
                ClassName=self.MESSAGE_LIST, searchDepth=self.SEARCH_DEPTH
            )
        except Exception as exc:
            raise WeChatUnavailable(
                "找不到消息列表。可能不在聊天页（通讯录/搜索/设置页），请切回目标群聊。"
            ) from exc
        return [(c.ClassName, c.Name) for c in msg_list.GetChildren()]

    # ---------- 会话列表：枚举 / 自动切群 ----------

    MASTER = "mmui::ChatMasterView"
    SESSION_TABLE = "mmui::XTableView"
    TAB_WECHAT = "mmui::XTabBarItem"

    def _master(self):
        assert self._wnd is not None
        mv = self._wnd.Control(ClassName="mmui::MainView", searchDepth=8, foundIndex=2)
        if not mv.Exists(1):
            mv = self._wnd.Control(ClassName="mmui::MainView", searchDepth=8)
        return mv.Control(ClassName=self.MASTER, searchDepth=10)

    def _table(self):
        return self._master().Control(ClassName=self.SESSION_TABLE, searchDepth=10)

    @staticmethod
    def _cell_name(cell) -> str:
        """会话项 Name 是多行：「群名\n[8条] 某某: 内容\n17:48\n…」，首行才是会话名。"""
        return ((getattr(cell, "Name", "") or "").split("\n")[0] or "").strip()

    @staticmethod
    def _wheel(table, notches: int) -> None:
        """在会话列表上发真实滚轮。

        Qt 自绘的 XTableView 不实现 UIA ScrollPattern（GetPattern 返回 None），
        uiautomation 的 WheelDown 又只对当前鼠标位置生效 —— 所以只能把光标
        移到列表中央再发 MOUSEEVENTF_WHEEL。副作用是挪了用户的光标，结束复原。
        """
        try:
            import ctypes
            from ctypes import wintypes

            u = ctypes.windll.user32
            rect = table.BoundingRectangle
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            old = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(old))
            u.SetCursorPos(cx, cy)
            step = 120 * 3
            for _ in range(abs(notches)):
                u.mouse_event(0x0800, 0, 0, step if notches > 0 else -step, 0)
            u.SetCursorPos(old.x, old.y)
        except Exception:  # pragma: no cover - 纯 UI 副作用，失败不影响主流程
            pass

    def activate(self) -> bool:
        """把微信窗口提到前台并聚焦。

        为什么必须做：cell.Click() 走的是**屏幕坐标**上的模拟鼠标点击，
        不是 UIA 的 Invoke 模式。微信窗口被别的程序盖住时，点击会落到
        遮挡它的窗口上，微信什么也收不到 —— 而 UIA 照样返回成功，
        title() 也照样能读（UIA 读控件树不需要前台）。

        实测踩坑（2026-10-20）：用户在 WorkBuddy 里点「立即抓取」，
        微信被压在后面，open_chat 明明找到了群、也「点」了，
        群名却纹丝不动，日志只会报「会话列表里找不到这个群」误导排查。
        """
        assert self._wnd is not None
        try:
            hwnd = self._wnd.NativeWindowHandle
            u = ctypes.windll.user32
            # IsIconic：最小化的窗口先还原，否则 SetForegroundWindow 不生效
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.25)
            if u.GetForegroundWindow() == hwnd:
                return True
            # 直接 SetForegroundWindow 常被系统前台锁拒绝（返回 0 但不抛），
            # 走一遍 AttachThreadInput 借用前台线程的输入队列，成功率高得多。
            fg = u.GetForegroundWindow()
            tid_fg = u.GetWindowThreadProcessId(fg, None)
            tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
            attached = False
            if tid_fg and tid_fg != tid_me:
                attached = bool(u.AttachThreadInput(tid_fg, tid_me, True))
            try:
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    u.AttachThreadInput(tid_fg, tid_me, False)
            time.sleep(0.3)
            return u.GetForegroundWindow() == hwnd
        except Exception:  # pragma: no cover - 纯 UI 副作用
            return False

    def switch_to_chat_tab(self) -> bool:
        """微信可能停在通讯录/收藏页 —— 那些页面没有会话列表，先切回「微信」。"""
        assert self._wnd is not None
        try:
            tab = self._wnd.Control(ClassName=self.TAB_WECHAT, searchDepth=12, Name="微信")
            if tab.Exists(0.5):
                tab.Click(simulateMove=False)
                return True
        except Exception:
            pass
        return False

    def sessions(self, max_pages: int = 10) -> list[str]:
        """枚举会话列表里的会话名（按当前排序）。

        列表是虚拟化的：GetChildren() 只返回当前可见项，所以要一屏一屏滚。
        连续两屏没有新名字就认为到底了。只读，不改动任何会话。
        """
        table = self._table()
        if not table.Exists(1):
            return []
        names: list[str] = []
        seen: set[str] = set()
        stale = 0
        for page in range(max_pages):
            fresh = 0
            try:
                cells = table.GetChildren()
            except Exception:
                break
            for cell in cells:
                name = self._cell_name(cell)
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
                    fresh += 1
            if page >= 1 and fresh == 0:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
            self._wheel(table, -4)
            time.sleep(0.45)
        return names

    def open_chat(self, expected: str, max_pages: int = 10) -> str:
        """在会话列表里找到 expected 并点进去，返回点进去之后读到的群名。

        找不到就返回空串 —— 调用方据此决定是否报「群不存在」。
        这里不做搜索框输入：微信的搜索框是 Qt 自绘控件，UIA SetValue / SendInput
        写进去都不生效（实测），只有点会话项这条路径可靠。
        """
        table = self._table()
        if not table.Exists(1):
            return ""

        def try_click() -> str:
            try:
                cells = table.GetChildren()
            except Exception:
                return ""
            for cell in cells:
                name = self._cell_name(cell)
                if name and group_matches(name, expected):
                    cell.Click(simulateMove=False)
                    for _ in range(10):
                        time.sleep(0.3)
                        got = self.title()
                        if group_matches(got, expected):
                            return got
                    return got
            return ""

        # 先在当前可见的几屏里找（群来了新消息就在顶上，绝大多数情况一次命中）
        hit = try_click()
        if hit:
            return hit

        # 群沉下去了：滚回顶部再一屏一屏往下找
        self._wheel(table, 40)
        time.sleep(0.6)
        for _ in range(max_pages):
            hit = try_click()
            if hit:
                return hit
            self._wheel(table, -4)
            time.sleep(0.45)
        return ""

    def close(self) -> None:
        self._wnd = None


class WeChatCollector:
    """轮询采集：新增消息 → 提取单号 → 入队。

    两条通道分层去重，这是整个采集器最关键的设计：

    文本消息：靠指纹基线。群里说的话是流水，只能看「新增」那几条，
      否则第一次运行就会把一周前的旧单号全灌进队列。

    文件消息：**不走基线**。消息气泡本身不含单号，要去微信接收目录找实体文件
      再解析。这里靠「内容 sha256」去重（imports 表），性质是幂等的 ——
      同一份表被处理一次和十次，结果完全一样。所以首轮就能把当前屏的表格
      采下来，不需要等第二次轮询，也不会因为重启而重复入库。
      文件名+大小只负责「找到是哪一份文件」，不作为去重依据。
    """

    SEEN_KEY = "wechat.seen"
    SEEN_MAX = 500
    # 自动切群的退避：失败后 30s 起翻倍，最长 5 分钟一次。
    # 目标群不存在时不能每 2.5 秒去滚一遍会话列表 —— 那会把用户鼠标抢疯。
    NAV_MIN_BACKOFF = 30.0
    NAV_MAX_BACKOFF = 300.0

    def __init__(
        self,
        config,
        service,
        reader: WeChatReader | None = None,
        locator: WeChatFileLocator | None = None,
        rules=None,
    ) -> None:
        self.config = config
        self.service = service
        self.reader = reader or WeChatReader()
        self.locator = locator or WeChatFileLocator(config.wechat.file_root)
        # 规则可从界面实时改：有 RuleStore 就每轮读它，没有就退回配置文件
        self.rules = rules
        self._loaded = False
        self._has_baseline = False
        self._seen: list[str] = []
        self._handled_files: set[str] = set()
        # 自动切群：上次尝试的时刻 + 当前退避间隔 + 界面要显示的最近状态
        self._nav_at = 0.0
        self._nav_backoff = self.NAV_MIN_BACKOFF
        self.state = "未开始"
        # 采集间隔（秒）与「下一次采集的时刻」。
        # 间隔由界面设定（0 = 实时）：自动切群、翻会话列表都会挪鼠标，
        # 不想被打扰时把间隔调大，采集线程就只按点醒一次。
        # 这两个值由采集线程每轮写入，HTTP 层只读，界面据此显示倒计时。
        self.interval_sec = int(getattr(config.wechat, "interval_sec", 0) or 0)
        self.next_at = 0.0  # epoch 秒；0 = 没有排期（马上采 / 未运行）
        # 界面 → 采集线程的请求通道。
        # UIA 只能在初始化过 COM 的那个线程里调用（HTTP 线程里直接调会抛
        # «CoInitialize has not been called»），所以「列会话 / 打开群」这类
        # 操作必须排进采集线程执行。
        self._req: queue.Queue = queue.Queue()

    def serve_requests(self) -> None:
        """在采集线程里把界面发来的 UIA 请求做完（每轮轮询前调一次）。"""
        while True:
            try:
                kind, arg, fut = self._req.get_nowait()
            except queue.Empty:
                return
            if not fut.set_running_or_notify_cancel():  # 调用方已经超时放弃
                continue
            try:
                if kind == "sessions":
                    self.reader.open()
                    try:
                        self.reader.switch_to_chat_tab()
                        time.sleep(0.3)
                        fut.set_result(self.reader.sessions())
                    finally:
                        self.reader.close()
                elif kind == "open":
                    self.reader.open()
                    try:
                        self.reader.switch_to_chat_tab()
                        time.sleep(0.3)
                        got = self.reader.open_chat(str(arg))
                        if got:
                            self._nav_backoff = self.NAV_MIN_BACKOFF
                            self.state = f"已切换到「{got}」"
                        fut.set_result(got)
                    finally:
                        self.reader.close()
                elif kind == "collect":
                    # 「立即抓取」用的：在采集线程里**当场**跑一轮 poll_once。
                    # 为什么必须排进这个队列：poll_once 全程走 UIA，只能在
                    # 初始化过 COM 的采集线程里调，HTTP 线程直接调会抛
                    # «CoInitialize has not been called»。
                    # force_nav=True：用户点按钮时正在等结果，切群不能等退避。
                    # 这里不重试也不吞异常 —— 失败原因原样回给界面，
                    # 用户要能看到「到底是群没找到、还是微信没开」。
                    stats = self.poll_once(force_nav=True)
                    fut.set_result(stats)
                else:
                    fut.set_exception(WeChatUnavailable(f"未知的微信请求：{kind}"))
            except Exception as exc:  # noqa: BLE001 - 原样回传给 HTTP 层
                fut.set_exception(exc)

    def submit(self, kind: str, arg: object = None, timeout: float = 30.0):
        """给采集线程派活并等结果（HTTP 线程调用）。"""
        if not self._req:
            raise WeChatUnavailable("采集器未初始化")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._req.put((kind, arg, fut))
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            raise WeChatUnavailable(
                "微信没有响应：采集线程可能在忙（或服务未运行）。稍后再试。"
            ) from exc

    def _auto_open(self, expected: str, current: str, force: bool = False) -> str:
        """当前不在采集群时，自动在会话列表里点进去。

        有退避：失败一次后 30s 内不再动微信；连续失败翻倍到 5 分钟。
        成功后立刻把退避复位 —— 群换回来时下一轮就能正常工作。

        force=True 时**无视退避**。为什么需要：退避是防止后台轮询每 2.5 秒
        滚一遍会话列表抢用户鼠标；但「立即抓取」是用户亲手点的，那时候
        退避期内直接放弃会让按钮看起来完全没反应（实测就踩了这个坑：
        群明明在会话列表第 4 行，却因为 30 秒退避窗口而报「仍未进入该群」）。
        """
        now = time.monotonic()
        if not force and now - self._nav_at < self._nav_backoff:
            return current
        self._nav_at = now
        log.info(
            "不在采集群，尝试自动打开群聊",
            extra={
                "status": f"current={current or '未知'} target={expected}"
                + ("（手动触发，跳过退避）" if force else "")
            },
        )
        try:
            # 先把微信提到前台：后面的点击是屏幕坐标模拟，窗口被盖住就白点
            self.reader.activate()
            self.reader.switch_to_chat_tab()
            time.sleep(0.4)
            got = self.reader.open_chat(expected)
        except Exception as exc:  # noqa: BLE001 - 切群失败不该打断采集线程
            log.warning("自动打开群聊出错", extra={"status": f"{type(exc).__name__}: {exc}"})
            got = ""
        if got and group_matches(got, expected):
            self._nav_backoff = self.NAV_MIN_BACKOFF
            self.state = f"已自动切换到「{got}」"
            log.info("已自动切换到采集群", extra={"status": got})
            return got
        self._nav_backoff = min(self._nav_backoff * 2, self.NAV_MAX_BACKOFF)
        self.state = f"找不到群「{expected}」"
        log.warning(
            "自动打开群聊失败：会话列表里找不到这个群",
            extra={"status": f"target={expected} next_retry={self._nav_backoff:.0f}s"},
        )
        return got

    @property
    def prefixes(self) -> tuple[str, ...]:
        return self.rules.prefixes if self.rules is not None else self.config.scan.prefixes

    @property
    def lengths(self) -> tuple[int, ...]:
        return self.rules.lengths if self.rules is not None else self.config.scan.lengths

    @property
    def group_name(self) -> str:
        """要采集的群名。界面上可改，下一轮轮询立刻按新值校验。"""
        if self.rules is not None:
            return self.rules.group_name
        return self.config.wechat.group_name

    @property
    def enabled(self) -> bool:
        if self.rules is not None:
            return self.rules.enabled
        return self.config.wechat.enabled

    @property
    def window(self) -> tuple[str, str, str]:
        """(模式, 起始日, 结束日)。模式 today / range / all。"""
        if self.rules is not None:
            return self.rules.window
        cfg = self.config.wechat
        return (cfg.window, cfg.window_start, cfg.window_end)

    def _in_window(self, when: datetime | None, assume_today: bool = False) -> bool:
        """日期是否落在采集范围内。

        when 为空＝屏幕最上面那段没有时间分隔条，推不出日期。此时：
          · 整屏连一个分隔条都没有 → 当作今天（活跃群里看到的基本都是当天）
          · 有分隔条但这条在更上面 → 说明比分隔条更早，保守跳过
        「宁可少采，也不要把昨天的单再扫一遍」——扫描即产生真实轨迹。
        """
        mode, start, end = self.window
        if mode == "all":
            return True
        if when is None:
            if not assume_today:
                return False
            day: date | None = date.today()
        else:
            day = when.date()
        if mode == "today":
            return day == date.today()
        lo = _parse_day(start)
        hi = _parse_day(end)
        if lo and day < lo:
            return False
        if hi and day > hi:
            return False
        return True

    def _load_seen(self) -> None:
        if self._loaded:
            return
        with self.service._conn() as conn:  # noqa: SLF001 - 基线就存在 runtime 表
            from ..db import get_runtime

            raw = get_runtime(conn, self.SEEN_KEY, "")
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                self._seen = list(data.get("seen") or [])
                self._has_baseline = True
            elif isinstance(data, list):  # 兼容早期只存数组的写法
                self._seen = data
                self._has_baseline = True
        self._loaded = True

    def _save_seen(self) -> None:
        from ..db import set_runtime, utcnow

        self._seen = self._seen[-self.SEEN_MAX:]
        payload = json.dumps({"seen": self._seen, "at": utcnow()}, ensure_ascii=False)
        with self.service._conn() as conn:  # noqa: SLF001
            set_runtime(conn, self.SEEN_KEY, payload)

    def _ingest_files(self, messages: list[Message]) -> dict[str, int]:
        """把屏幕上的表格消息落成实体文件 → 解析 → 入队。

        返回 {"files": 实际导入份数, "waybills": 入队条数,
              "skipped": 已导入过, "missing": 本地没找到}
        """
        from ..waybill import rules_signature
        from .excel import extract_file, file_digest

        # 本轮用同一份规则快照，避免中途被人改规则导致前后不一致
        prefixes, lengths = self.prefixes, self.lengths
        # 去重键 = 内容哈希 + 识别规则指纹：规则改了（比如新增 621 前缀）
        # 旧表会被重新解析一次，否则新规则对已导入的文件永远不生效。
        rules = rules_signature(prefixes, lengths)

        stats = {"files": 0, "waybills": 0, "skipped": 0, "missing": 0, "out_of_window": 0}
        for msg in messages:
            if msg.kind != "file":
                continue
            ref = parse_file_bubble(msg.text)
            if ref is None:
                continue
            key = f"{ref.name}|{ref.kb}"
            if key in self._handled_files:
                continue
            path = self.locator.find(ref)
            if path is None:
                # 不记入 handled：可能还在下载，下一轮再试。
                # 必须 warning：debug 打在正常日志里看不见，2026-10-29 就是这样
                # 静默丢了 98 单（群里文件与磁盘名对不上，界面上毫无提示）。
                stats["missing"] += 1
                log.warning(
                    "微信文件在本地找不到（没下载完，或磁盘文件名与消息不一致）",
                    extra={"status": f"{ref.name} ({ref.kb}K) root={self.locator.root}"},
                )
                continue

            # 文件按落盘时间判日期：微信下载文件的时间就是消息时间附近
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            if not self._in_window(mtime):
                stats["out_of_window"] += 1
                continue

            digest = file_digest(path)
            import_ref = f"{digest}#{rules}"
            if self.service.import_seen(IMPORT_KIND, import_ref):
                self._handled_files.add(key)
                stats["skipped"] += 1
                continue

            try:
                result = extract_file(path, prefixes, lengths)
            except Exception as exc:  # noqa: BLE001 - 单个文件坏了不能拖垮整轮
                self._handled_files.add(key)
                log.error(
                    "微信文件解析失败", extra={"status": f"{path.name}: {exc}"}
                )
                continue

            enq = None
            if result.waybills:
                enq = self.service.enqueue(
                    result.waybills, source="wx", source_ref=ref.name, actor="wechat"
                )
            self.service.record_import(
                IMPORT_KIND,
                import_ref,
                ref.name,
                len(result.waybills),
                enq.inserted if enq else 0,
                enq.duplicated if enq else 0,
            )
            self._handled_files.add(key)
            stats["files"] += 1
            stats["waybills"] += enq.inserted if enq else 0
            log.info(
                "微信文件入库",
                extra={
                    "status": (
                        f"{path.name} cells={result.cells_scanned} "
                        f"waybills={len(result.waybills)} inserted={enq.inserted if enq else 0}"
                    )
                },
            )
        return stats

    def poll_once(self, force_nav: bool = False) -> dict[str, int]:
        """采一轮。

        force_nav=True 是「立即抓取」按钮用的：切群时无视退避窗口，
        因为那一刻是用户在等结果，不是后台轮询。
        """
        from ..waybill import extract

        expected = self.group_name
        if not self.enabled:
            raise WeChatUnavailable("微信采集已在界面上关闭")
        if not expected:
            raise WeChatUnavailable("还没设置采集群（界面上「采集群」填一下群名）")

        self.reader.open()
        try:
            title = self.reader.title()
            if not group_matches(title, expected):
                # 微信停在了别的会话（或压根没打开任何会话）：先自己点进目标群。
                # 这是「开服务就全自动」的关键一步 —— 不让用户手工把微信切到群上。
                title = self._auto_open(expected, title, force=force_nav)
                if not group_matches(title, expected):
                    self.state = f"当前会话是「{title or '未知'}」"
                    raise WeChatUnavailable(
                        f"当前会话是「{title or '未知'}」，不是配置的群"
                        f"「{expected}」。已尝试自动打开但仍未进入该群，"
                        "请确认群名是否正确（可在设置中心点「读取微信会话」选群）。"
                    )
            self.state = f"采集中「{title}」"
            raw = self.reader.read_items()
            # 整屏有没有时间分隔条：没有的话，推不出日期的消息按「今天」处理
            has_separator = any(is_time_separator(name) for _, name in raw if name)
            messages = parse_items(raw)
        finally:
            self.reader.close()

        # 文件通道先跑，且不受基线影响（靠内容哈希去重，天然幂等）
        file_stats = self._ingest_files(messages)

        self._load_seen()

        if not self._has_baseline:
            # 首次运行（或 runtime 被清空）：只记录文本基线，不采历史。
            #
            # 这是有意的保护：扫描即产生真实物流轨迹，宁可少采也不能把群里
            # 昨天/早上的历史单重新揽一遍。代价是「首次启动时屏幕上已有的文本
            # 消息不会被采集」——这个代价是明确的，界面上要提示用户。
            text_only = [m for m in messages if m.kind == "text"]
            self._seen = [m.fingerprint for m in text_only][-self.SEEN_MAX:]
            self._has_baseline = True
            self._save_seen()
            log.info(
                "微信采集基线已建立",
                extra={"status": f"messages={len(messages)} 跳过文本={len(text_only)}"},
            )
            return {
                "new": 0,
                "inserted": file_stats["waybills"],
                "baselined": 1,
                "files": file_stats["files"],
                "skipped": file_stats["skipped"],
                "missing": file_stats["missing"],
                "out_of_window": file_stats["out_of_window"],
            }

        seen = set(self._seen)
        text_msgs = [m for m in messages if m.kind == "text" and m.fingerprint not in seen]
        # 只采窗口内的消息（默认「仅当天」）。这是为了防止把群里的历史单再扫一遍。
        fresh = [m for m in text_msgs if self._in_window(m.when, assume_today=not has_separator)]
        out_of_window = len(text_msgs) - len(fresh)
        waybills: list[str] = []
        for msg in fresh:
            waybills.extend(extract(msg.text, self.prefixes, self.lengths))

        inserted = file_stats["waybills"]
        if waybills:
            result = self.service.enqueue(
                waybills, source="wx", source_ref=expected, actor="wechat"
            )
            inserted += result.inserted

        if fresh:
            self._seen.extend(m.fingerprint for m in fresh)
            self._save_seen()
        if fresh or file_stats["files"]:
            log.info(
                "微信采集",
                extra={
                    "status": (
                        f"new={len(fresh)} waybills={len(waybills)} inserted={inserted} "
                        f"files={file_stats['files']} out_of_window={out_of_window}"
                    )
                },
            )
        return {
            "new": len(fresh),
            "out_of_window": out_of_window + file_stats["out_of_window"],
            "inserted": inserted,
            "baselined": 0,
            "files": file_stats["files"],
            "skipped": file_stats["skipped"],
            "missing": file_stats["missing"],
        }
        return {"new": len(fresh), "inserted": inserted, "baselined": 0}
