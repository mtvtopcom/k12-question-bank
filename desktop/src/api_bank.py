#!/usr/bin/env python3
"""本地题库：JSONL 追加存储、去重、筛选、多格式导出。

两套独立的库，各占一个 JSONL 文件，一行一道题，便于外部程序流式读取：
- ``api``：大模型 API 任务生成的题，每条带 task_id，可按任务切换查看；
- ``all``：全部题库，只装用户导入的题和从 API 库一键合并过来的题。

去重：API 库按 id（同一任务重跑不重复入库）；全部题库按「题干 + 选项」的
内容指纹（同一道题换个 id、换个来源也不会重复进来）。

导出支持 JSON / TXT / CSV / TSV / XLSX，全部用标准库实现（XLSX 直接写 OOXML）。
导出的学科、年级写中文，难度写「容易/中等/较难/难」，本软件导入时能原样读回。
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import unicodedata
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import api_config

BANK_API = "api"
BANK_ALL = "all"
BANK_LABELS = {BANK_API: "API 任务题库", BANK_ALL: "全部题库"}

# 导出用的中文名：文件给老师看，直接写「数学 / 五年级 / 中等」比代码好认
SUBJECT_NAMES = {"chinese": "语文", "math": "数学", "english": "英语"}
STAGE_NAMES = {"kg": "幼儿园", "primary": "小学", "junior": "初中"}
GRADE_NAMES = {
    "k1": "小班", "k2": "中班", "k3": "大班",
    "g1": "一年级", "g2": "二年级", "g3": "三年级", "g4": "四年级",
    "g5": "五年级", "g6": "六年级", "g7": "七年级", "g8": "八年级", "g9": "九年级",
}
DIFFICULTY_NAMES = {1: "容易", 2: "中等", 3: "较难", 4: "难"}
TYPE_NAMES = {"single_choice": "单选题"}

# (记录字段, 导出表头)
TABLE_COLUMNS: List[Tuple[str, str]] = [
    ("id", "题目ID"),
    ("topic_id", "知识点ID"),
    ("topic_name", "知识点"),
    ("subject", "学科"),
    ("grade", "年级"),
    ("stage", "学段"),
    ("volume_title", "册"),
    ("chapter", "单元"),
    ("domain", "领域"),
    ("difficulty", "难度"),
    ("bloom", "认知层级"),
    ("type", "题型"),
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

_cache: Dict[str, Dict[str, Any]] = {}
_fp_cache: Dict[str, Any] = {"key": {}, "set": {}}

_BAD_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

# 归一化时去掉的标点（保留 + - * / = 等算式符号，避免「3-1」与「31」误判重复）
_NORM_DROP = re.compile(
    "[\u3000\\s,，。.、;；:：!！?？'\"“”‘’()（）\\[\\]【】{}《》〈〉"
    "—－_·…]+")


def safe_filename(name: Any, fallback: str = "任务") -> str:
    """任务名 → 可用的文件名（去掉路径分隔符与非法字符，限长 80）。"""
    text = _BAD_FILENAME.sub("-", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip().strip(".").strip()
    return (text[:80].strip().strip(".") or fallback)


# ---------------- 去重指纹 ----------------
def _norm_text(value: Any) -> str:
    """题干 / 选项归一化：全角转半角、小写、去空白与句读标点。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return _NORM_DROP.sub("", text)


def fingerprint(rec: Dict[str, Any]) -> str:
    """内容指纹 = 归一化题干 + 归一化选项（按 A~D 顺序）。"""
    options = rec.get("options") or []
    if not isinstance(options, (list, tuple)):
        options = []
    parts = [_norm_text(rec.get("stem"))]
    parts += [_norm_text(o) for o in list(options)[:4]]
    raw = "\x1f".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------- 读写 ----------------
def _path(bank: str) -> str:
    return api_config.bank_path(bank)


def _key(bank: str) -> Optional[Tuple[int, int]]:
    path = _path(bank)
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load(bank: str = BANK_API, force: bool = False) -> List[Dict[str, Any]]:
    """按文件顺序返回某个库的全部题目（旧 → 新）；解析失败的行跳过。"""
    slot = _cache.setdefault(bank, {"key": None, "rows": []})
    key = _key(bank)
    if not force and key == slot["key"]:
        return list(slot["rows"])
    rows: List[Dict[str, Any]] = []
    path = _path(bank)
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
    slot["key"] = key
    slot["rows"] = rows
    return list(rows)


def existing_ids(bank: str = BANK_API, force: bool = False) -> set:
    return {r.get("id", "") for r in load(bank, force=force) if r.get("id")}


def existing_fingerprints(bank: str = BANK_ALL,
                          force: bool = False) -> Set[str]:
    """某库已有的内容指纹集合（按文件 mtime/size 缓存，避免重复计算）。"""
    key = _key(bank)
    if not force and _fp_cache["key"].get(bank) == key and _fp_cache["set"].get(bank):
        return set(_fp_cache["set"][bank])
    fps = {fingerprint(r) for r in load(bank, force=force)}
    _fp_cache["key"][bank] = key
    _fp_cache["set"][bank] = fps
    return set(fps)


def rows_for_task(task_id: str) -> List[Dict[str, Any]]:
    """某个任务生成的全部题目（API 库，文件顺序，旧 → 新）。"""
    if not task_id:
        return []
    return [r for r in load(BANK_API) if r.get("task_id") == task_id]


def append(records: Sequence[Dict[str, Any]], known: Optional[set] = None,
           bank: str = BANK_API, known_fps: Optional[Set[str]] = None) -> int:
    """追加题目并去重；known / known_fps 可传入已知集合，避免重复读文件。

    API 库按 id 去重；全部题库同时按内容指纹去重（同一道题不同来源只留一份）。
    """
    if not records:
        return 0
    known_ids = existing_ids(bank) if known is None else set(known)
    if known_fps is not None:
        known_fps = set(known_fps)
    elif bank == BANK_ALL:
        known_fps = existing_fingerprints(bank)
    else:
        known_fps = set()
    fresh: List[Dict[str, Any]] = []
    for r in records:
        rid = r.get("id")
        if not rid or rid in known_ids:
            continue
        fp = fingerprint(r)
        if bank == BANK_ALL and fp in known_fps:
            continue
        known_ids.add(rid)
        known_fps.add(fp)
        fresh.append(r)
    if not fresh:
        return 0
    path = _path(bank)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in fresh:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _cache[bank] = {"key": None, "rows": []}
    _fp_cache["key"][bank] = None
    return len(fresh)


def _backup(bank: str = BANK_API) -> str:
    """破坏性操作前备份题库，每个库最多保留 20 份。"""
    path = _path(bank)
    if not os.path.exists(path):
        return ""
    folder = os.path.join(api_config.data_dir(), "backups")
    os.makedirs(folder, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    prefix = "all-questions" if bank == BANK_ALL else "questions"
    dest = os.path.join(folder, f"{prefix}-{stamp}.jsonl")
    shutil.copy2(path, dest)
    old = sorted(f for f in os.listdir(folder) if f.startswith(prefix + "-"))
    for name in old[:-20]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass
    return dest


def _rewrite(rows: Iterable[Dict[str, Any]], bank: str = BANK_API) -> None:
    path = _path(bank)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in rows:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _cache[bank] = {"key": None, "rows": []}
    _fp_cache["key"][bank] = None


def delete(ids: Iterable[str], bank: str = BANK_API) -> int:
    """删除指定 id 的题目；返回删除条数。"""
    targets = {str(i) for i in ids}
    if not targets:
        return 0
    rows = load(bank, force=True)
    keep = [r for r in rows if r.get("id") not in targets]
    removed = len(rows) - len(keep)
    if removed:
        _backup(bank)
        _rewrite(keep, bank)
    return removed


def delete_by_task(task_id: str) -> int:
    """删除某个 API 任务生成的全部题目（只动 API 库）。"""
    if not task_id:
        return 0
    rows = load(BANK_API, force=True)
    keep = [r for r in rows if r.get("task_id") != task_id]
    removed = len(rows) - len(keep)
    if removed:
        _backup(BANK_API)
        _rewrite(keep, BANK_API)
    return removed


def clear(bank: str = BANK_API) -> int:
    rows = load(bank, force=True)
    if not rows:
        return 0
    _backup(bank)
    path = _path(bank)
    try:
        os.remove(path)
    except OSError:
        _rewrite([], bank)
    _cache[bank] = {"key": None, "rows": []}
    _fp_cache["key"][bank] = None
    return len(rows)


# ---------------- 查询 ----------------
def stage_of(rec: Dict[str, Any]) -> str:
    """题目归属学段：优先用记录里的 stage，老记录按年级前缀推断。"""
    stage = str(rec.get("stage") or "")
    if stage:
        return stage
    grade = str(rec.get("grade") or "")
    if grade.startswith("k"):
        return "kg"
    if grade.startswith("g"):
        try:
            return "primary" if int(grade[1:]) <= 6 else "junior"
        except ValueError:
            return ""
    return ""


def query(q: str = "", subject: str = "", grade: str = "", stage: str = "",
          difficulty: Optional[int] = None, topic_id: str = "",
          task_id: str = "", bank: str = BANK_API,
          limit: int = 0) -> List[Dict[str, Any]]:
    """筛选题目，按生成时间倒序（最新在前）。"""
    text = (q or "").strip().lower()
    rows = []
    for rec in reversed(load(bank)):
        if task_id and str(rec.get("task_id") or "") != task_id:
            continue
        if subject and rec.get("subject") != subject:
            continue
        if grade and rec.get("grade") != grade:
            continue
        if stage and stage_of(rec) != stage:
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


def stats(bank: str = BANK_API) -> Dict[str, Any]:
    rows = load(bank)
    today = dt.date.today().isoformat()
    by_subject: Dict[str, int] = {}
    by_task: Dict[str, int] = {}
    topics = set()
    for rec in rows:
        by_subject[rec.get("subject", "")] = by_subject.get(rec.get("subject", ""), 0) + 1
        tid = str(rec.get("task_id") or "")
        if tid:
            by_task[tid] = by_task.get(tid, 0) + 1
        if rec.get("topic_id"):
            topics.add(rec["topic_id"])
    return {"total": len(rows), "topics": len(topics),
            "today": sum(1 for r in rows if str(r.get("created_at", "")).startswith(today)),
            "by_subject": by_subject, "by_task": by_task}


def task_counts() -> Dict[str, int]:
    """API 库里每个任务的题目数（任务 id → 题数）。"""
    return dict(stats(BANK_API)["by_task"])


# ---------------- 导出 ----------------
def _flat(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\t\r\n]+", " ", str(value)).strip()


def _field_value(rec: Dict[str, Any], key: str, options: Sequence[Any]) -> str:
    """导出时把内部代码换成中文，保证文件打开就能看懂。"""
    if key in ("option_a", "option_b", "option_c", "option_d"):
        index = "abcd".index(key[-1])
        return _flat(options[index] if index < len(options) else "")
    if key == "subject":
        code = str(rec.get("subject") or "")
        return SUBJECT_NAMES.get(code, code)
    if key == "grade":
        label = str(rec.get("grade_label") or "")
        return label or GRADE_NAMES.get(str(rec.get("grade") or ""),
                                        str(rec.get("grade") or ""))
    if key == "stage":
        code = stage_of(rec)
        return STAGE_NAMES.get(code, code)
    if key == "difficulty":
        value = rec.get("difficulty")
        try:
            return DIFFICULTY_NAMES.get(int(value), str(value if value is not None else ""))
        except (TypeError, ValueError):
            return _flat(value)
    if key == "type":
        text = str(rec.get("type") or "")
        return TYPE_NAMES.get(text, text)
    return _flat(rec.get(key))


def to_matrix(rows: Sequence[Dict[str, Any]]) -> List[List[str]]:
    matrix: List[List[str]] = []
    for rec in rows:
        options = rec.get("options") or []
        if not isinstance(options, (list, tuple)):
            options = []
        options = list(options) + [""] * (4 - len(options))
        matrix.append([_field_value(rec, key, options)
                       for key, _label in TABLE_COLUMNS])
    return matrix


def to_dicts(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """JSON 导出：一条题一个对象，字段用中文表头，导入时能原样读回。"""
    labels = [label for _key, label in TABLE_COLUMNS]
    return [dict(zip(labels, line)) for line in to_matrix(rows)]


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


# TXT 里出现的元信息行（顺序固定，一行一项，紧随题干之前，中间不能空行）
_TXT_META = (("知识点", "topic_name"), ("学科", "subject"), ("年级", "grade"),
             ("册", "volume_title"), ("单元", "chapter"), ("难度", "difficulty"),
             ("认知层级", "bloom"), ("题型", "type"), ("来源", "source"),
             ("模型", "model"))


def to_txt_text(rows: Sequence[Dict[str, Any]]) -> str:
    """TXT 导出：每题一段，段间空行；元信息在题干之前，导入时原样读回。"""
    blocks: List[str] = []
    for rec in rows:
        options = rec.get("options") or []
        if not isinstance(options, (list, tuple)):
            options = []
        lines: List[str] = []
        for label, key in _TXT_META:
            value = _field_value(rec, key, list(options) + [""] * 4)
            if value:
                lines.append(f"【{label}】{value}")
        lines.append(_flat(rec.get("stem")))
        for i, opt in enumerate(list(options)[:4]):
            lines.append(f"{'ABCD'[i]}. {_flat(opt)}")
        answer = _flat(rec.get("answer"))
        if answer:
            lines.append(f"答案：{answer}")
        explanation = _flat(rec.get("explanation"))
        if explanation:
            lines.append(f"解析：{explanation}")
        blocks.append("\n".join(line for line in lines if line))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


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
    """按格式写文件，返回写入的题目数。

    支持 JSON / TXT / CSV / TSV / XLSX；不再提供 JSONL（用户反馈用不上）。
    """
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
    if fmt == "txt":
        with open(path, "w", encoding="utf-8") as f:
            f.write(to_txt_text(rows))
        return len(rows)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(to_dicts(rows), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return len(rows)
