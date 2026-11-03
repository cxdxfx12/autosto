"""和「隔壁那个也在操作手机的程序」打招呼：动手前叫它让一下。

## 为什么要有这个文件

这台电脑上同时跑着两个程序，都在用 adb 操作同一部手机：

- **本项目**（运单揽件助手）：盯申行者 `cn.sto.sxz.core.app`，批量录入单号 + 点确定 + 上传；
- **钉钉微信转发桥**（`../ddwx-bridge`）：盯钉钉，靠手机上的无障碍服务
  定时「自愈」——发现前台不是钉钉就**打开最近任务、把钉钉拉回来、搜索并点开目标群**。

两个程序互不认识，各干各的都对，但凑在一起就出事。2026-10-20 的实际现场：

    18:44:57  我们注入 176 条成功（输入框实收 176 条 ✓）
    18:44:57  紧接着点「确定」—— 对方巡检正好在这一刻把钉钉拉到前台
    18:44:57  读列表序号 = 0 / 预期 176 → 中止（好在没误上传）
    18:48:30  这 176 条被人当垃圾清掉了，只能人工补点

不是谁的 bug，是**没人协调**。这个模块就是那段协调。

## 谁让谁

让**它**让路。理由：我们的动作是秒级的一小段（注入→确定→上传），
断了就整批作废；它的巡检是每 5/10/15 分钟一次的例行公事，晚一轮完全无害。
所以由我们开口、它挪开，代价最小。

## 怎么让

不碰对方进程、不碰手机 APK，只调它网关的一个 HTTP 接口：

    POST http://127.0.0.1:8765/watch-pause  {"seconds": 180}

它收到后这段时间里不再下发巡检指令，手机端的巡检定时器随之停掉。
到点自动恢复 —— 我们这边就算崩了也不会把它们永久关死（这是刻意的设计：
依赖「对方一定会来解除」的锁，是最容易变成事故的锁）。

## 对方没开怎么办

**必须能当它不存在。** 对方没启动时端口连不上，这里只记一条日志就返回，
绝不抛异常 —— 揽收不能被「隔壁没开」这种事挡住。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

# 对方网关：config.yaml 里 gateway_port: 8765，只连本机。
PEER_PORT = 8765
PEER_HOST = "127.0.0.1"

# 默认让路时长（秒）。取 180：够 500 条整批注入+确定+上传走完（实测约 60-90 秒），
# 又短到万一我们异常退出，对方最多瞎 3 分钟就能自己回来。
DEFAULT_SECONDS = 180


def _config_candidates() -> tuple[Path, ...]:
    """找 ddwx-bridge 的 config.yaml（只读它取 token）。

    安装版里 tools 被塞进 core\\_internal\\tools，"上两级找兄弟目录"那套推导
    就失效了 —— 让路会静默退化成「跳过」，手机和钉钉桥重新抢前台。
    所以这里按可靠性排序：显式配置 → 程序根附近 → 源码结构 → 本机固定布局。
    """
    out: list[Path] = []
    env = os.environ.get("STO_BRIDGE_CONFIG", "").strip()
    if env:
        out.append(Path(env))
    root = os.environ.get("MIAO_ROOT", "").strip()
    if root:
        base = Path(root).resolve()
        out += [
            base.parent / "ddwx-bridge" / "config.yaml",
            base / "ddwx-bridge" / "config.yaml",
        ]
    out.append(Path(__file__).resolve().parent.parent.parent / "ddwx-bridge" / "config.yaml")
    # 本机部署：两个项目都在 D:\wxauto-main 下（源码态上面已经命中，这里是安装版兜底）
    out.append(Path("D:/wxauto-main/ddwx-bridge/config.yaml"))
    return tuple(out)


CONFIG_CANDIDATES = _config_candidates()


def _peer_token() -> str:
    """从对方 config.yaml 里取 token（只读，不改）。

    对方把 token 写在 `server.token` 或类似的键下；这里做宽松扫描，
    找不到就返回空串，接口会 401，我们记一条日志就好。
    """
    for path in CONFIG_CANDIDATES:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("token:") or s.startswith("gateway_token:"):
                return s.split(":", 1)[1].strip().strip("'\"")
    return ""


def _post(path: str, payload: dict, timeout: float = 3.0) -> tuple[bool, str]:
    """POST 到对方网关。绕代理（127.0.0.1 走系统代理会变成 502，踩过）。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://{PEER_HOST}:{PEER_PORT}{path}",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Bridge-Token": _peer_token(),
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
        try:
            return True, str(json.loads(body).get("message") or body[:120])
        except ValueError:
            return True, body[:120]
    except socket.timeout:
        return False, "对方网关超时"
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "token 对不上（对方 config.yaml 里的 token 变了？）"
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"连不上（{reason}）"


def ask_peer_yield(seconds: int = DEFAULT_SECONDS) -> bool:
    """请对方让路。成功（或对方根本没开）都返回 True —— 不该挡住揽收。

    返回 False 只在「对方开着但拒绝了」这种值得人看一眼的情况下出现。
    """
    ok, msg = _post("/watch-pause", {"seconds": int(seconds)})
    if ok:
        print(f"      [让路] 钉钉桥已让路 {seconds}s：{msg}")
        return True
    # 对方没开是最常见的情况，不值得用警告吓人。
    if "连不上" in msg:
        print(f"      [让路] 钉钉桥没在跑，跳过（{msg}）")
        return True
    print(f"      [让路] ！请求失败：{msg}（继续跑，但可能和它抢前台）")
    return False


def release_peer() -> None:
    """提前解除让路。跑完就还回去，别让对方白等满 3 分钟。"""
    ok, msg = _post("/watch-pause", {"seconds": 0})
    if ok:
        print(f"      [让路] 已解除，钉钉桥恢复巡检：{msg}")
    else:
        # 解除失败不影响我们（到点它会自己恢复），只提示一下。
        print(f"      [让路] 解除失败（{msg}），对方到点会自行恢复")


if __name__ == "__main__":
    import sys

    secs = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS
    if secs <= 0:
        release_peer()
    else:
        ask_peer_yield(secs)
