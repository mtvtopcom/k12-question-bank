#!/usr/bin/env python3
"""题库导入与合并：JSON / JSONL / CSV / TSV / XLSX / TXT → 全部题库。

三件事：
1. 解析：按扩展名选解析器，XLSX 用标准库直接读 OOXML，不依赖第三方包；
2. 归一：中英文表头都能认（题干/question、选项A/option_a…），学科、年级、
   难度尽量转成内部代码，转不了就原样保留；
3. 排重：按「题干 + 选项」的内容指纹，与全部题库已有的题、以及本批内部
   互相排重；题干选项相同但答案不同的计为「冲突」，同样过滤掉。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

import api_bank

SUPPORTED_EXTS = (".json", ".jsonl", ".csv", ".tsv", ".txt", ".xlsx", ".xlsm")

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

SUBJECT_TO_CODE = {
    "语文": "chinese", "数学": "math", "英语": "english",
    "chinese": "chinese", "math": "math", "english": "english",
}
GRADE_TO_CODE = {
    "小班": "k1", "中班": "k2", "大班": "k3",
    "一年级": "g1", "二年级": "g2", "三年级": "g3", "四年级": "g4",
    "五年级": "g5", "六年级": "g6", "七年级": "g7", "八年级": "g8",
    "九年级": "g9", "初一": "g7", "初二": "g8", "初三": "g9",
    "1年级": "g1", "2年级": "g2", "3年级": "g3", "4年级": "g4",
    "5年级": "g5", "6年级": "g6", "7年级": "g7", "8年级": "g8", "9年级": "g9",
    "k1": "k1", "k2": "k2", "k3": "k3",
    "g1": "g1", "g2": "g2", "g3": "g3", "g4": "g4", "g5": "g5",
    "g6": "g6", "g7": "g7", "g8": "g8", "g9": "g9",
    "1": "g1", "2": "g2", "3": "g3", "4": "g4", "5": "g5",
    "6": "g6", "7": "g7", "8": "g8", "9": "g9",
}
GRADE_LABELS = {
    "k1": "小班", "k2": "中班", "k3": "大班",
    "g1": "一年级", "g2": "二年级", "g3": "三年级", "g4": "四年级",
    "g5": "五年级", "g6": "六年级", "g7": "七年级", "g8": "八年级", "g9": "九年级",
}
DIFFICULTY_TO_CODE = {
    "容易": 1, "简单": 1, "基础": 1, "1": 1,
    "中等": 2, "适中": 2, "2": 2,
    "较难": 3, "偏难": 3, "3": 3,
    "难": 4, "困难": 4, "4": 4,
    "easy": 1, "medium": 2, "hard": 4,
}

# 表头别名（比较前统一转小写、去空格与下划线，全角转半角）
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "id": ("题目id", "题号", "编号", "id"),
    "topic_id": ("知识点id", "topicid", "pointid"),
    "topic_name": ("知识点", "知识点名称", "考点", "topic", "topicname"),
    "subject": ("学科", "科目", "subject"),
    "grade": ("年级", "年级名称", "grade", "gradelabel"),
    "chapter": ("单元", "章节", "chapter", "unit"),
    "difficulty": ("难度", "难度(1-4)", "难度等级", "difficulty", "level"),
    "bloom": ("认知层级", "认知层次", "bloom"),
    "stem": ("题干", "题目", "问题", "试题", "题目内容", "题干内容",
             "question", "stem", "content"),
    "option_a": ("选项a", "a", "a项", "选项1", "optiona", "opta"),
    "option_b": ("选项b", "b", "b项", "选项2", "optionb", "optb"),
    "option_c": ("选项c", "c", "c项", "选项3", "optionc", "optc"),
    "option_d": ("选项d", "d", "d项", "选项4", "optiond", "optd"),
    "options_raw": ("选项", "选项列表", "options", "optionlist"),
    "answer": ("答案", "正确答案", "参考答案", "answer", "key"),
    "answer_text": ("答案内容", "正确选项内容", "answertext"),
    "explanation": ("解析", "答案解析", "试题解析", "解答", "详解",
                    "explanation", "analysis"),
    "source": ("来源", "出处", "source"),
    "model": ("模型", "model"),
    "created_at": ("生成时间", "创建时间", "时间", "createdat", "created"),
    "type": ("题型", "type", "questiontype"),
    "domain": ("领域", "domain"),
    "volume_title": ("册", "教材", "volumetitle"),
}

_HEADER_LOOKUP: Dict[str, str] = {}


def _hkey(text: Any) -> str:
    """表头归一：NFKC、小写、去空白/下划线/连接符、去括号单位。"""
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    s = re.sub(r"[\s_\-]+", "", s)
    s = re.sub(r"[（(].*?[)）]", "", s)
    return s


_HEADER_LOOKUP = {}
for _canon, _names in _ALIASES.items():
    for _name in _names:
        _HEADER_LOOKUP.setdefault(_hkey(_name), _canon)


@dataclass
class ImportResult:
    """一次导入 / 合并的结果。"""
    files: int = 0
    total: int = 0            # 读到的记录数（含无效）
    added: int = 0
    duplicate: int = 0
    conflict: int = 0         # 题干选项相同但答案不同
    invalid: int = 0          # 缺题干等无法入库
    errors: List[str] = field(default_factory=list)

    def summary(self, title: str = "导入完成") -> str:
        lines = [f"{title}：新增 {self.added} 题"]
        if self.duplicate:
            extra = f"，其中答案冲突 {self.conflict} 题" if self.conflict else ""
            lines.append(f"重复已过滤 {self.duplicate} 题{extra}")
        if self.invalid:
            lines.append(f"缺题干 / 无法识别 {self.invalid} 条")
        if self.errors:
            lines.append("；".join(self.errors[:3]) +
                         ("…" if len(self.errors) > 3 else ""))
        return "\n".join(lines)


# ---------------- 文本与字段 ----------------
def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"[\r\n\t]+", " ", str(value)).strip()


def _read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _answer_key(value: Any) -> str:
    """答案归一：'A.' '答案是B' → 'A' / 'B'；其他内容原样。"""
    s = _clean(value)
    if not s:
        return ""
    m = re.fullmatch(r"(?:答案[是为:：]?\s*)?[（(]?\s*([A-Da-d])\s*[)）]?[.。、]?", s)
    if m:
        return m.group(1).upper()
    return s


def _strip_option_prefix(text: str) -> str:
    return re.sub(r"^\s*[（(]?[A-Da-d][)）.．、:：]\s*", "", str(text or "")).strip()


def _split_inline_options(stem: str) -> Tuple[str, List[str]]:
    """从题干里拆出 A. B. C. D. 选项（仅在没有选项列时兜底）。"""
    marks = list(re.finditer(r"(?:^|[\s；;，,])([A-D])[.．、)）]\s*", stem))
    if len(marks) < 2:
        return stem, []
    letters = [m.group(1) for m in marks]
    if "A" not in letters or "B" not in letters:
        return stem, []
    start = marks[0].start()
    opts: List[str] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(stem)
        opts.append(stem[m.end():end].strip(" \t；;，,"))
    return stem[:start].strip(), opts


def _to_record(raw: Dict[str, Any], filename: str, now: str) -> Optional[Dict[str, Any]]:
    """把一行原始数据转成内部记录；缺题干返回 None。"""
    data: Dict[str, Any] = {}
    for key, value in raw.items():
        canon = _HEADER_LOOKUP.get(_hkey(key))
        if not canon:
            continue
        if canon == "options_raw":
            data["options_raw"] = value
        elif data.get(canon) in (None, ""):
            data[canon] = value

    stem = _clean(data.get("stem"))
    if not stem:
        return None

    options: List[str] = []
    for letter in "abcd":
        opt = _clean(data.get(f"option_{letter}"))
        if opt:
            options.append(_strip_option_prefix(opt))
    if not options and data.get("options_raw"):
        raw_opts = data["options_raw"]
        if isinstance(raw_opts, (list, tuple)):
            parts = [_clean(o) for o in raw_opts]
        else:
            parts = [_clean(p) for p in re.split(r"[\r\n|｜]+", str(raw_opts))]
        options = [_strip_option_prefix(p) for p in parts if p]
    if not options:
        stem, options = _split_inline_options(stem)
    options = [o for o in options[:4]]

    subject_raw = _clean(data.get("subject"))
    subject_key = re.sub(r"[（(].*?[)）]", "", subject_raw).strip()
    subject = SUBJECT_TO_CODE.get(subject_key,
                                  SUBJECT_TO_CODE.get(subject_key.lower(), subject_raw))
    grade_raw = _clean(data.get("grade"))
    grade_key = re.sub(r"[（(].*?[)）]", "", grade_raw)
    grade_key = re.sub(r"(?:上|下)?册?$", "", grade_key).strip() or grade_raw
    grade = GRADE_TO_CODE.get(grade_raw, GRADE_TO_CODE.get(grade_key, ""))
    if not grade and grade_raw.startswith(("g", "k")) and grade_raw[1:].isdigit():
        grade = grade_raw.lower()

    diff_raw = _clean(data.get("difficulty")).lower()
    difficulty = DIFFICULTY_TO_CODE.get(diff_raw, "")
    if not difficulty:
        m = re.search(r"([1-4])", diff_raw)
        difficulty = int(m.group(1)) if m else ""

    answer = _answer_key(data.get("answer"))
    if len(options) >= 2 and answer in {"1", "2", "3", "4"}:
        answer = "ABCD"[int(answer) - 1]      # 用 1~4 表示选项的题库
    rec: Dict[str, Any] = {
        "stem": stem,
        "options": options,
        "answer": answer,
        "answer_text": _clean(data.get("answer_text")),
        "explanation": _clean(data.get("explanation")),
        "subject": subject,
        "grade": grade,
        "grade_label": GRADE_LABELS.get(grade, grade_raw),
        "topic_id": _clean(data.get("topic_id")),
        "topic_name": _clean(data.get("topic_name")),
        "chapter": _clean(data.get("chapter")),
        "difficulty": difficulty,
        "bloom": _clean(data.get("bloom")),
        "type": _clean(data.get("type")),
        "domain": _clean(data.get("domain")),
        "volume_title": _clean(data.get("volume_title")),
        "source": _clean(data.get("source")) or "导入",
        "model": _clean(data.get("model")),
        "created_at": _clean(data.get("created_at")) or now,
        "origin": "import",
        "origin_file": os.path.basename(filename),
        "imported_at": now,
    }
    for extra in ("id", "task_id"):
        value = _clean(data.get(extra))
        if value:
            rec[f"origin_{extra}"] = value
    fp = api_bank.fingerprint(rec)
    rec["fp"] = fp
    rec["id"] = f"imp-{fp}"
    return rec


# ---------------- 各格式解析 ----------------
def _header_row(matrix: Sequence[Sequence[Any]]) -> int:
    """前 10 行里找命中已知表头最多的一行，兼容上方有标题/说明的表格。"""
    best, best_hits = 0, -1
    for i, row in enumerate(matrix[:10]):
        hits = sum(1 for value in row if _hkey(value) in _HEADER_LOOKUP)
        if hits > best_hits:
            best, best_hits = i, hits
    return best if best_hits >= 2 else 0


def _rows_from_matrix(matrix: Sequence[Sequence[Any]], filename: str,
                      now: str) -> Tuple[List[Dict[str, Any]], int]:
    """二维表 → 记录；返回 (记录, 无效行数)。"""
    if not matrix:
        return [], 0
    start = _header_row(matrix)
    header = [_clean(h) for h in matrix[start]]
    records: List[Dict[str, Any]] = []
    invalid = 0
    for line in matrix[start + 1:]:
        if not any(_clean(v) for v in line):
            continue
        row = {}
        for i, value in enumerate(line):
            if i < len(header) and header[i]:
                row[header[i]] = value
        rec = _to_record(row, filename, now)
        if rec is None:
            invalid += 1
        else:
            records.append(rec)
    return records, invalid


def _parse_delimited(text: str, delimiter: str) -> List[List[str]]:
    buf = io.StringIO(text, newline="")
    try:
        return [row for row in csv.reader(buf, delimiter=delimiter)]
    except csv.Error:
        return []


def parse_json(path: str, now: str) -> Tuple[List[Dict[str, Any]], int]:
    data = json.loads(_read_text(path))
    if isinstance(data, dict):
        for key in ("questions", "data", "items", "list", "rows"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            if all(isinstance(v, dict) for v in data.values()) and data:
                data = list(data.values())
            else:
                data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON 顶层既不是数组，也不含 questions/data 列表")
    records, invalid = [], 0
    for item in data:
        if not isinstance(item, dict):
            invalid += 1
            continue
        rec = _to_record(item, path, now)
        if rec is None:
            invalid += 1
        else:
            records.append(rec)
    return records, invalid


def parse_jsonl(path: str, now: str) -> Tuple[List[Dict[str, Any]], int]:
    records, invalid = [], 0
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if not isinstance(item, dict):
            invalid += 1
            continue
        rec = _to_record(item, path, now)
        if rec is None:
            invalid += 1
        else:
            records.append(rec)
    return records, invalid


def parse_csv(path: str, now: str, delimiter: str = ",") -> Tuple[List[Dict[str, Any]], int]:
    text = _read_text(path)
    first = text.split("\n", 1)[0]
    if delimiter == "," and "," not in first and "\t" in first:
        delimiter = "\t"          # 后缀写成 .csv、实际用制表符分隔的文件
    return _rows_from_matrix(_parse_delimited(text, delimiter), path, now)


def parse_tsv(path: str, now: str) -> Tuple[List[Dict[str, Any]], int]:
    return parse_csv(path, now, delimiter="\t")


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return -1
    index = 0
    for ch in letters.group(1):
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def parse_xlsx(path: str, now: str) -> Tuple[List[Dict[str, Any]], int]:
    """最小 OOXML 读取：共享字符串 + 首个工作表，够读常见题库表格。"""
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{{{_MAIN_NS}}}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_MAIN_NS}}}t")))

        sheet_path = "xl/worksheets/sheet1.xml"
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            first = wb.find(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet")
            rel_id = first.get(f"{{{_REL_NS}}}id") if first is not None else None
            if rel_id:
                rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
                for rel in rels.findall(f"{{{_PKG_REL_NS}}}Relationship"):
                    if rel.get("Id") == rel_id:
                        target = (rel.get("Target") or "").lstrip("/")
                        sheet_path = target if target.startswith("xl/") else "xl/" + target
                        break
        if sheet_path not in names:
            candidates = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
            if not candidates:
                raise ValueError("工作簿里找不到工作表")
            sheet_path = candidates[0]

        sheet = ET.fromstring(zf.read(sheet_path))
        matrix: List[List[str]] = []
        for row in sheet.iter(f"{{{_MAIN_NS}}}row"):
            cells: Dict[int, str] = {}
            for cell in row.findall(f"{{{_MAIN_NS}}}c"):
                idx = _col_index(cell.get("r"))
                if idx < 0:
                    continue
                kind = cell.get("t")
                if kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.iter(f"{{{_MAIN_NS}}}t"))
                else:
                    v = cell.find(f"{{{_MAIN_NS}}}v")
                    value = v.text if v is not None and v.text is not None else ""
                    if kind == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            value = ""
                    elif kind == "b":
                        value = "TRUE" if value == "1" else "FALSE"
                cells[idx] = value
            if cells:
                width = max(cells) + 1
                matrix.append([cells.get(i, "") for i in range(width)])
    return _rows_from_matrix(matrix, path, now)


_ANSWER_LINE = re.compile(
    r"^\s*[【\[]?\s*(?:正确)?答案\s*[】\]]?\s*"
    r"(?!解析|与解析|和解析)[：:]?\s*(.+)$")
_EXPLAIN_LINE = re.compile(
    r"^\s*(?:[【\[]\s*(?:解析|答案解析|试题解析|解答|详解|分析)\s*[】\]]\s*[：:]?"
    r"|(?:解析|答案解析|试题解析|解答|详解|分析)\s*[：:])\s*(.*)$")
_META_LINE = re.compile(
    r"^\s*[【\[]?\s*(学科|年级|知识点|单元|难度|认知层级|题型|来源|模型)"
    r"\s*[】\]]?\s*[：:]?\s*(.+)$")
_OPT_MARK = re.compile(r"^\s*[（(]?([A-Da-d])[)）.．、:：]\s*(.*)$")


def _split_option_line(line: str) -> List[Tuple[str, str]]:
    """一行里可能挤了多个选项：「A. 红 B. 绿」也拆开。"""
    marks = list(re.finditer(r"(?:^|\s)[（(]?([A-Da-d])[)）.．、:：]\s*", line))
    if len(marks) <= 1:
        m = _OPT_MARK.match(line)
        return [(m.group(1).upper(), m.group(2).strip())] if m else []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(line)
        out.append((m.group(1).upper(), line[m.end():end].strip(" \t；;，,")))
    return out


def parse_txt(path: str, now: str) -> Tuple[List[Dict[str, Any]], int]:
    """TXT 按空行分块：题干 / A. B. C. D. / 答案：X / 解析：…"""
    text = _read_text(path).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n+", text)
    records, invalid = [], 0
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        stem_lines: List[str] = []
        options: List[str] = []
        answer = ""
        explanation: List[str] = []
        meta: Dict[str, str] = {}
        mode = "stem"
        for line in lines:
            if mode != "explain":
                m = _META_LINE.match(line)
                if m and not stem_lines:
                    meta[m.group(1)] = m.group(2).strip()
                    continue
            m = _ANSWER_LINE.match(line)
            if m:
                answer = m.group(1).strip()
                mode = "answer"
                continue
            m = _EXPLAIN_LINE.match(line)
            if m:
                explanation.append(m.group(1).strip())
                mode = "explain"
                continue
            if mode in ("stem", "options"):
                opts = _split_option_line(line)
                if opts:
                    options.extend(text_ for _letter, text_ in opts)
                    mode = "options"
                    continue
            if mode == "explain":
                explanation.append(line)
            elif mode == "options" and options:
                options[-1] = (options[-1] + " " + line).strip()
            elif mode == "answer":
                explanation.append(line)       # 答案后面没写「解析：」的说明文字
            else:
                stem_lines.append(line)
        raw = {
            "题干": " ".join(stem_lines),
            "选项A": options[0] if len(options) > 0 else "",
            "选项B": options[1] if len(options) > 1 else "",
            "选项C": options[2] if len(options) > 2 else "",
            "选项D": options[3] if len(options) > 3 else "",
            "答案": answer,
            "解析": " ".join(explanation),
        }
        for key, value in meta.items():
            raw[key] = value
        rec = _to_record(raw, path, now)
        if rec is None:
            invalid += 1
        else:
            records.append(rec)
    return records, invalid


def parse_file(path: str, now: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int]:
    """按扩展名解析单个文件，返回 (记录, 无效行数)。"""
    now = now or dt.datetime.now().isoformat(timespec="seconds")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        return parse_json(path, now)
    if ext == ".jsonl":
        return parse_jsonl(path, now)
    if ext == ".csv":
        return parse_csv(path, now)
    if ext == ".tsv":
        return parse_tsv(path, now)
    if ext in (".xlsx", ".xlsm"):
        return parse_xlsx(path, now)
    if ext == ".txt":
        return parse_txt(path, now)
    if ext == ".xls":
        raise ValueError("暂不支持旧版 .xls，请在 Excel/WPS 里另存为 .xlsx 再导入")
    raise ValueError(f"不支持的格式：{ext or '未知'}")


# ---------------- 导入 / 合并 ----------------
def _existing_index() -> Dict[str, Dict[str, Any]]:
    """全部题库现有内容指纹 → 记录（用于排重与冲突统计）。"""
    index: Dict[str, Dict[str, Any]] = {}
    for rec in api_bank.load(api_bank.BANK_ALL):
        index.setdefault(api_bank.fingerprint(rec), rec)
    return index


def import_paths(paths: Sequence[str]) -> ImportResult:
    """把若干文件导入全部题库，跨文件、与已有内容统一排重。"""
    result = ImportResult()
    index = _existing_index()
    existing_fps = set(index)
    batch: List[Dict[str, Any]] = []
    for path in paths:
        result.files += 1
        try:
            records, invalid = parse_file(path)
        except Exception as exc:  # noqa: BLE001 —— 单个文件失败不影响其他文件
            result.errors.append(f"{os.path.basename(path)}：{type(exc).__name__} {exc}")
            continue
        result.total += len(records) + invalid
        result.invalid += invalid
        for rec in records:
            fp = rec.get("fp") or api_bank.fingerprint(rec)
            old = index.get(fp)
            if old is not None:
                result.duplicate += 1
                if _answer_key(old.get("answer")) != _answer_key(rec.get("answer")):
                    result.conflict += 1
                continue
            index[fp] = rec
            batch.append(rec)
    if batch:
        result.added = api_bank.append(
            batch, bank=api_bank.BANK_ALL, known_fps=existing_fps)
    return result


def merge_from_api(task_id: str = "") -> ImportResult:
    """把 API 任务题库（全部或某个任务）合并进全部题库，按内容排重。"""
    result = ImportResult()
    index = _existing_index()
    existing_fps = set(index)
    batch: List[Dict[str, Any]] = []
    now = dt.datetime.now().isoformat(timespec="seconds")
    source = api_bank.load(api_bank.BANK_API)
    for rec in source:
        if task_id and str(rec.get("task_id") or "") != task_id:
            continue
        result.total += 1
        fp = api_bank.fingerprint(rec)
        old = index.get(fp)
        if old is not None:
            result.duplicate += 1
            if _answer_key(old.get("answer")) != _answer_key(rec.get("answer")):
                result.conflict += 1
            continue
        copy = dict(rec)
        copy["origin"] = "api"
        copy["origin_task_id"] = str(rec.get("task_id") or "")
        copy["imported_at"] = now
        copy["fp"] = fp
        index[fp] = copy
        batch.append(copy)
    if batch:
        result.added = api_bank.append(
            batch, bank=api_bank.BANK_ALL, known_fps=existing_fps)
    return result
