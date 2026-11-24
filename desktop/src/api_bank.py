#!/usr/bin/env python3
"""本地题库：JSONL 追加存储、去重、筛选、多格式导出。

题库文件位于用户数据目录（questions.jsonl），一行一道题，便于外部程序流式读取。
导出支持 JSON / JSONL / CSV / TSV / XLSX，全部用标准库实现（XLSX 直接写 OOXML）。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import api_config

# (记录字段, 导出表头)
TABLE_COLUMNS: List[Tuple[str, str]] = [
    ("id", "题目ID"),
    ("topic_id", "知识点ID"),
    ("topic_name", "知识点"),
    ("subject", "学科"),
    ("grade", "年级"),
    ("chapter", "单元"),
    ("difficulty", "难度(1-4)"),
    ("bloom", "认知层级"),
    ("stem", "题干"),
    ("option_a", "选项A"),
    ("option_b", "选项B"),
    ("option_c", "选项C"),
    ("option_d", "选项D"),
    ("answer", "答案"),
    ("answer_text", "答案内容"),
    ("explanation", "解析"),
    ("source", "来源"),
    ("model", "模型"),
    ("created_at", "生成时间"),
]

_cache: Dict[str, Any] = {"key": None, "rows": []}

_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(name: Any, fallback: str = "任务") -> str:
    """任务名 → 可用的文件名（去掉路径分隔符与非法字符，限长 80）。"""
    text = _BAD_FILENAME.sub("-", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    return (text[:80].strip().strip(".") or fallback)


# ---------------- 读写 ----------------
def _key() -> Optional[Tuple[int, int]]:
    path = api_config.bank_path()
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load(force: bool = False) -> List[Dict[str, Any]]:
    """按文件顺序返回全部题目（旧 → 新）；解析失败的行跳过。"""
    key = _key()
    if not force and key == _cache["key"]:
        return list(_cache["rows"])
    rows: List[Dict[str, Any]] = []
    path = api_config.bank_path()
    if key is not None:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("stem"):
                    rows.append(obj)
    _cache["key"] = key
    _cache["rows"] = rows
    return list(rows)


def existing_ids(force: bool = False) -> set:
    return {r.get("id", "") for r in load(force=force) if r.get("id")}


def rows_for_task(task_id: str) -> List[Dict[str, Any]]:
    """某个任务生成的全部题目（文件顺序，旧 → 新）。"""
    if not task_id:
        return []
    return [r for r in load() if r.get("task_id") == task_id]


def append(records: Sequence[Dict[str, Any]], known: Optional[set] = None) -> int:
    """追加题目，按 id 去重；known 可传入已知 id 集合避免重复读文件。"""
    if not records:
        return 0
    known_ids = existing_ids() if known is None else known
    fresh = [r for r in records if r.get("id") and r["id"] not in known_ids]
    if not fresh:
        return 0
    path = api_config.bank_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in fresh:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _cache["key"] = None
    return len(fresh)


def _backup() -> str:
    """破坏性操作前备份题库，最多保留 20 份。"""
    path = api_config.bank_path()
    if not os.path.exists(path):
        return ""
    folder = os.path.join(api_config.data_dir(), "backups")
    os.makedirs(folder, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = os.path.join(folder, f"questions-{stamp}.jsonl")
    shutil.copy2(path, dest)
    old = sorted(f for f in os.listdir(folder) if f.startswith("questions-"))
    for name in old[:-20]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    return dest


def _rewrite(rows: Iterable[Dict[str, Any]]) -> None:
    path = api_config.bank_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in rows:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _cache["key"] = None
    _cache["rows"] = []


def delete(ids: Iterable[str]) -> int:
    """删除指定 id 的题目；返回删除条数。"""
    targets = {str(i) for i in ids}
    if not targets:
        return 0
    rows = load(force=True)
    keep = [r for r in rows if r.get("id") not in targets]
    removed = len(rows) - len(keep)
    if removed:
        _backup()
        _rewrite(keep)
    return removed


def clear() -> int:
    rows = load(force=True)
    if not rows:
        return 0
    _backup()
    path = api_config.bank_path()
    try:
        os.remove(path)
    except OSError:
        _rewrite([])
    _cache["key"] = None
    _cache["rows"] = []
    return len(rows)


# ---------------- 查询 ----------------
def query(q: str = "", subject: str = "", grade: str = "",
          difficulty: Optional[int] = None, topic_id: str = "",
          limit: int = 0) -> List[Dict[str, Any]]:
    """筛选题目，按生成时间倒序（最新在前）。"""
    text = (q or "").strip().lower()
    rows = []
    for rec in reversed(load()):
        if subject and rec.get("subject") != subject:
            continue
        if grade and rec.get("grade") != grade:
            continue
        if difficulty and int(rec.get("difficulty") or 0) != int(difficulty):
            continue
        if topic_id and rec.get("topic_id") != topic_id:
            continue
        if text:
            haystack = " ".join(str(rec.get(k, "")) for k in
                                ("id", "topic_id", "topic_name", "stem", "explanation"))
            if text not in haystack.lower():
                continue
        rows.append(rec)
        if limit and len(rows) >= limit:
            break
    return rows


def stats() -> Dict[str, Any]:
    rows = load()
    today = dt.date.today().isoformat()
    by_subject: Dict[str, int] = {}
    topics = set()
    for rec in rows:
        by_subject[rec.get("subject", "")] = by_subject.get(rec.get("subject", ""), 0) + 1
        if rec.get("topic_id"):
            topics.add(rec["topic_id"])
    return {"total": len(rows), "topics": len(topics),
            "today": sum(1 for r in rows if str(r.get("created_at", "")).startswith(today)),
            "by_subject": by_subject}


# ---------------- 导出 ----------------
def _flat(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\t\r\n]+", " ", str(value)).strip()


def to_matrix(rows: Sequence[Dict[str, Any]]) -> List[List[Any]]:
    matrix: List[List[Any]] = []
    for rec in rows:
        options = rec.get("options") or []
        if not isinstance(options, (list, tuple)):
            options = []
        options = list(options) + [""] * (4 - len(options))
        line: List[Any] = []
        for key, _label in TABLE_COLUMNS:
            if key in ("option_a", "option_b", "option_c", "option_d"):
                line.append(options["abcd".index(key[-1])])
            elif key == "difficulty":
                try:
                    line.append(int(rec.get("difficulty") or 0))
                except (TypeError, ValueError):
                    line.append("")
            else:
                line.append(rec.get(key, ""))
        matrix.append(line)
    return matrix


def to_csv_text(rows: Sequence[Dict[str, Any]], delimiter: str = ",",
                bom: bool = True) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL,
                        lineterminator="\r\n")
    writer.writerow([label for _key, label in TABLE_COLUMNS])
    for line in to_matrix(rows):
        writer.writerow([_flat(v) for v in line])
    text = buf.getvalue()
    return ("\ufeff" + text) if bom else text


# ---- 最小 XLSX 写入（OOXML，无第三方依赖） ----
def _col_name(index: int) -> str:
    """1 → A，27 → AA。"""
    name = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _ILLEGAL_XML.sub("", text)
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _disp_width(value: Any) -> int:
    text = str(value or "")
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def _sheet_name(name: str) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "", name or "Sheet1").strip() or "Sheet1"
    return name[:31]


def write_xlsx(path: str, rows: Sequence[Dict[str, Any]]) -> int:
    """写一个可被 Excel / Numbers / WPS 打开的 .xlsx，返回数据行数。"""
    headers = [label for _key, label in TABLE_COLUMNS]
    data = to_matrix(rows)

    def cell(ref: str, value: Any, header: bool = False) -> str:
        style = ' s="1"' if header else ""
        if isinstance(value, bool):
            value = str(value)
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"{style}><v>{value}</v></c>'
        raw = "" if value is None else str(value)
        keep = ' xml:space="preserve"' if raw != raw.strip() else ""
        return (f'<c r="{ref}"{style} t="inlineStr"><is><t{keep}>'
                f'{_xml_text(raw)}</t></is></c>')

    row_xml: List[str] = []
    header_cells = "".join(cell(f"{_col_name(i)}{1}", h, header=True)
                           for i, h in enumerate(headers, 1))
    row_xml.append(f'<row r="1" ht="22" customHeight="1">{header_cells}</row>')
    for ri, line in enumerate(data, 2):
        cells = "".join(cell(f"{_col_name(ci)}{ri}", value)
                        for ci, value in enumerate(line, 1))
        row_xml.append(f'<row r="{ri}">{cells}</row>')

    widths = []
    for ci, head in enumerate(headers):
        longest = max([_disp_width(head)] +
                      [_disp_width(line[ci]) for line in data[:300]])
        widths.append(min(60, max(9, longest + 2)))
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths, 1))

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        f'<cols>{cols}</cols>'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        '</worksheet>')

    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '</cellXfs></styleSheet>')

    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xml_text(_sheet_name("题库"))}" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>')

    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>')

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>')

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.styles+xml"/>'
        '</Types>')

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/styles.xml", styles)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return len(data)


def export(path: str, fmt: str, rows: Sequence[Dict[str, Any]]) -> int:
    """按格式写文件，返回写入的题目数。"""
    fmt = (fmt or "json").lower()
    if fmt == "xlsx":
        return write_xlsx(path, rows)
    if fmt == "csv":
        text = to_csv_text(rows, ",", bom=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return len(rows)
    if fmt == "tsv":
        text = to_csv_text(rows, "\t", bom=False)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return len(rows)
    if fmt == "jsonl":
        with open(path, "w", encoding="utf-8") as f:
            for rec in rows:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return len(rows)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(rows), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return len(rows)
