"""把上传的文件读成「表头 + 二维单元格」，不假设列名。

支持 CSV（自动探测编码与分隔符）与 XLSX（取第一个工作表）。
不引入 pandas：这些文件通常只有几万行，标准库足够，也少一个重依赖。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16")
DELIMITERS = (",", "\t", ";", "|")


@dataclass(slots=True)
class Table:
    headers: list[str]
    rows: list[list[str]]
    header_row: int
    meta: dict[str, object] = field(default_factory=dict)

    def dict_rows(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for row in self.rows:
            out.append({self.headers[i]: (row[i] if i < len(row) else "") for i in range(len(self.headers))})
        return out


def _decode(data: bytes) -> tuple[str, str]:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _looks_like_header(row: list[str]) -> bool:
    cells = [c for c in row if str(c).strip() != ""]
    if len(cells) < 2:
        return False
    # 表头通常以文字为主；数据行里数字/日期占比更高。
    texty = sum(1 for c in cells if any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in str(c)))
    return texty >= max(2, len(cells) // 2)


def _padded(rows: list[list[str]]) -> list[list[str]]:
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


def read_csv(data: bytes) -> Table:
    text, encoding = _decode(data)
    sample = text[:8192]
    delimiter = ","
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in DELIMITERS}
        delimiter = max(counts, key=counts.get) if any(counts.values()) else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    raw = [[str(cell).strip() for cell in row] for row in reader if any(str(c).strip() for c in row)]
    raw = _padded(raw)
    header_row = 0
    for index, row in enumerate(raw[:8]):
        if _looks_like_header(row):
            header_row = index
            break
    headers = raw[header_row]
    return Table(
        headers=headers,
        rows=raw[header_row + 1:],
        header_row=header_row,
        meta={"format": "csv", "encoding": encoding, "delimiter": delimiter, "lines": len(raw)},
    )


def read_xlsx(data: bytes) -> Table:
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = book.worksheets[0]
    raw: list[list[str]] = []
    for row in sheet.iter_rows(values_only=True):
        cells = [("" if cell is None else str(cell).strip()) for cell in row]
        if any(cells):
            raw.append(cells)
    book.close()
    raw = _padded(raw)
    header_row = 0
    for index, row in enumerate(raw[:8]):
        if _looks_like_header(row):
            header_row = index
            break
    return Table(
        headers=raw[header_row],
        rows=raw[header_row + 1:],
        header_row=header_row,
        meta={"format": "xlsx", "sheet": sheet.title, "lines": len(raw)},
    )


def read_table(data: bytes, filename: str) -> Table:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return read_xlsx(data)
    if suffix in {".csv", ".txt", ".tsv"}:
        return read_csv(data)
    raise ValueError(f"不支持的文件类型：{suffix or '未知'}（目前支持 .csv / .xlsx）")
