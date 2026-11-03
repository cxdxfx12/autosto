"""Excel / CSV 采集：不假设列名，整表扫。

为什么不按「运单号」列名取：业务员手里的表是别人给的，列名今天叫「运单号」，
明天叫「单号」，后天干脆没有表头。按列名取等于把失败写进代码里。
代价是要扫全表——50 行 × 10 列的量级，openpyxl 一秒内就扫完了。
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

from ..waybill import extract, extract_with_rejects, normalize_scalar

SUPPORTED = (".xlsx", ".xlsm", ".csv")

# 业务员发来的表常带一个被拉到 1048576 行的格式残留（openpyxl 会如实
# 报告 max_row=1048576，dims=A1:A1048576），但真实数据只有几百行。
# 不设短路的话，每轮轮询都要空转 100 万次，2.5 秒一次的采集直接被拖死。
# 连续这么多行全空就认为数据已结束——正常表格中间不会空出 200 行。
EMPTY_ROW_LIMIT = 200


@dataclass(frozen=True)
class Hit:
    waybill: str
    location: str  # "Sheet1!C7" / "a.csv:12"
    raw: str


@dataclass
class ExtractResult:
    path: Path
    digest: str
    hits: list[Hit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cells_scanned: int = 0

    @property
    def waybills(self) -> list[str]:
        # 同一份文件里重复出现的号只留一次
        return list(dict.fromkeys(h.waybill for h in self.hits))


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cell_text(value: object) -> tuple[str, str | None]:
    return normalize_scalar(value)


def _extract_xlsx(path: Path, prefixes: tuple[str, ...], lengths: tuple[int, ...]) -> ExtractResult:
    result = ExtractResult(path=path, digest=file_digest(path))
    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            empty_streak = 0
            for row in ws.iter_rows():
                if all(
                    c.value is None or isinstance(c.value, bool) for c in row
                ):
                    empty_streak += 1
                    if empty_streak >= EMPTY_ROW_LIMIT:
                        break
                    continue
                empty_streak = 0
                for cell in row:
                    result.cells_scanned += 1
                    value = cell.value
                    if value is None or isinstance(value, bool):
                        continue
                    text, warning = _cell_text(value)
                    if warning:
                        result.warnings.append(f"{ws.title}!{cell.coordinate}: {warning}")
                    if not text:
                        continue
                    hits, _rejects = extract_with_rejects(text, prefixes, lengths)
                    for wb_no in hits:
                        result.hits.append(Hit(wb_no, f"{ws.title}!{cell.coordinate}", text))
    finally:
        wb.close()
    return result


def _read_csv_rows(path: Path) -> list[list[str]]:
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            with path.open("r", encoding=encoding, newline="") as fh:
                sample = fh.read(2048)
                fh.seek(0)
                delimiter = ","
                if sample.count("\t") > sample.count(","):
                    delimiter = "\t"
                if sample.count(";") > sample.count(","):
                    delimiter = ";"
                return [row for row in csv.reader(fh, delimiter=delimiter)]
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文件编码：{path.name}（试过 utf-8 / gbk / utf-16）")


def _extract_csv(path: Path, prefixes: tuple[str, ...], lengths: tuple[int, ...]) -> ExtractResult:
    result = ExtractResult(path=path, digest=file_digest(path))
    for row_no, row in enumerate(_read_csv_rows(path), start=1):
        for col_no, value in enumerate(row, start=1):
            result.cells_scanned += 1
            text, warning = _cell_text(value)
            if warning:
                result.warnings.append(f"{path.name}:{row_no} 第{col_no}列 {warning}")
            if not text:
                continue
            hits, _ = extract_with_rejects(text, prefixes, lengths)
            for wb_no in hits:
                result.hits.append(Hit(wb_no, f"{path.name}:{row_no}", text))
    return result


def extract_file(path: Path, prefixes: tuple[str, ...], lengths: tuple[int, ...]) -> ExtractResult:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"不支持的表格类型：{suffix}（支持 {', '.join(SUPPORTED)}）")
    if suffix == ".csv":
        return _extract_csv(path, prefixes, lengths)
    return _extract_xlsx(path, prefixes, lengths)
