"""运单号识别：把「人能看懂的一坨文本」变成「一串确定的单号」。

这里只有纯函数，不碰数据库、不碰界面——所有关于「什么算单号」的分歧
都应当在这一层测试完，别散到采集器和扫描器里。

核心难点是**分隔符的两种含义**：
- `7712-3456-78901`、`7712 3456 7890 1` 里的分隔符是排版，要拼起来；
- `7712345678901 7712345678902` 里的空格是**两条单号**的边界，绝不能拼。
所以不能简单地「删掉所有空格」，而要先按空白切段，再按「拼起来是否超过最大长度」
贪心合并——长度上限本身就是最好的判据。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")

# 只删「夹在数字中间」的排版符（不含空白）。空白交给分组逻辑判断。
_WEAK_SEP = re.compile(r"(?<=\d)[\-–—_.,，。、'’]+(?=\d)")

_PARTS = re.compile(r"\d+|\D+")


@dataclass(frozen=True)
class Reject:
    raw: str
    reason: str


def _halfwidth(text: str) -> str:
    return text.translate(_FULLWIDTH)


def normalize_scalar(value: object) -> tuple[str, str | None]:
    """把单元格归一化为候选串。空白保留，交给 extract 统一处理。

    返回 (候选串, 警告)。警告只有一种：浮点大数可能已丢精度——程序救不了，
    只能提醒把源表那一列设成文本格式。
    """
    warning: str | None = None

    if isinstance(value, bool):
        return "", None
    if isinstance(value, int):
        return str(value), None
    if isinstance(value, float):
        if value.is_integer():
            text = str(int(value))
            if len(text) > 15:
                warning = f"{value!r} 是浮点数且超过 15 位，Excel 可能已丢精度，建议把该列设为文本格式"
            return text, warning
        return repr(value), f"{value!r} 是小数（疑似超过 15 位被 Excel 截断精度），请把该列设为文本格式"

    text = _halfwidth(str(value)).strip()
    text = _WEAK_SEP.sub("", text)
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text, warning


def _split_group(parts: list[str], max_len: int) -> list[str]:
    """把被空白分开的数字段合并成一个号，超过上限就断开。"""
    out: list[str] = []
    buf = ""
    for part in parts:
        if not buf:
            buf = part
            continue
        if len(buf) + len(part) <= max_len:
            buf += part
        else:
            out.append(buf)
            buf = part
    if buf:
        out.append(buf)
    return out


def _iter_candidates(text: str, lengths: tuple[int, ...]) -> list[str]:
    if not text:
        return []
    max_len = max(lengths) if lengths else 15
    normalized = _WEAK_SEP.sub("", _halfwidth(text))

    groups: list[list[str]] = []
    current: list[str] = []
    for part in _PARTS.findall(normalized):
        if part.isdigit():
            current.append(part)
        elif part.strip() == "":
            # 纯空白：先记着，下一段数字来了再看能不能拼
            continue
        else:
            if current:
                groups.append(current)
                current = []
    if current:
        groups.append(current)

    candidates: list[str] = []
    for group in groups:
        candidates.extend(_split_group(group, max_len))
    return candidates


def extract(text: str, prefixes: tuple[str, ...], lengths: tuple[int, ...]) -> list[str]:
    """从一段文本里提取所有合法单号，保持出现顺序并去重。"""
    hits: list[str] = []
    seen: set[str] = set()
    for candidate in _iter_candidates(text, lengths):
        if candidate in seen:
            continue
        if lengths and len(candidate) not in lengths:
            continue
        if prefixes and not candidate.startswith(tuple(prefixes)):
            continue
        seen.add(candidate)
        hits.append(candidate)
    return hits


def extract_with_rejects(
    text: str, prefixes: tuple[str, ...], lengths: tuple[int, ...]
) -> tuple[list[str], list[Reject]]:
    """同 extract，但额外返回「像单号却被前缀/长度挡掉」的候选。

    用于采集器日志：业务员说「我明明发了」时，能立刻回答是没认出来还是被规则挡了。
    """
    hits: list[str] = []
    rejects: list[Reject] = []
    seen: set[str] = set()
    min_len = min(lengths) if lengths else 1
    for candidate in _iter_candidates(text, lengths):
        if candidate in seen:
            continue
        if len(candidate) < min_len:
            continue  # 太短的是噪声，不值得记
        if lengths and len(candidate) not in lengths:
            rejects.append(Reject(candidate, f"长度 {len(candidate)} 不在允许范围 {lengths}"))
            continue
        if prefixes and not candidate.startswith(tuple(prefixes)):
            rejects.append(Reject(candidate, f"前缀不属于 {prefixes}"))
            continue
        seen.add(candidate)
        hits.append(candidate)
    return hits, rejects


def is_valid(waybill: str, prefixes: tuple[str, ...], lengths: tuple[int, ...]) -> bool:
    if not waybill.isdigit():
        return False
    if lengths and len(waybill) not in lengths:
        return False
    if prefixes and not waybill.startswith(tuple(prefixes)):
        return False
    return True


def rules_signature(prefixes: tuple[str, ...], lengths: tuple[int, ...]) -> str:
    """识别规则的指纹，参与导入去重。

    为什么要它：去重键如果只有文件内容哈希，规则改了以后旧表就永远补不进来。
    真实场景——一开始只认 `77`，`丹鸟.xlsx` 导进去 0 条；后来决定连丹鸟一起扫，
    加了 `621`。这时文件一个字节都没变，内容哈希完全相同，会被判成「已导入」
    而跳过，30 个丹鸟单号永远进不了队列。
    把规则指纹并进去重键，规则一变就自动重解析；单号唯一索引仍保证不会重复入队。
    """
    raw = "|".join((",".join(prefixes), ",".join(str(n) for n in lengths)))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
