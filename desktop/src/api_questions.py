#!/usr/bin/env python3
"""解析大模型返回的题目 JSON，并规范成题库记录。

模型输出常见的不规范情况都在这里兜住：
- 带 ```json 代码围栏、前后有解释文字、包一层 {"questions": [...]}
- 选项带「A.」前缀、答案是「A」/「选A」/选项原文、难度写成「中等」
- 混入 3 个或 5 个选项、重复选项、空解析等不合格题目 → 丢弃并给出原因
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import store

FENCE_RE = re.compile(r"```[ \t]*[a-zA-Z]*[ \t]*\r?\n?(.*?)```", re.S)
PREFIX_RE = re.compile(r"^\s*(?:[（(]\s*[A-Da-d]\s*[)）]|[A-Da-d]\s*[.、．:：])\s*")
DIFF_CN = {"容易": 1, "简单": 1, "基础": 1, "中等": 2, "较难": 3,
           "难": 4, "困难": 4, "挑战": 4}
BLOOM_EN = {"remember": "记忆", "understand": "理解", "apply": "应用",
            "analyze": "分析", "analyse": "分析", "evaluate": "评价", "create": "创造"}
BLOOM_CN = ("记忆", "理解", "应用", "分析", "评价", "创造")
LIST_KEYS = ("questions", "data", "items", "result", "list", "question")


class ParseError(Exception):
    pass


# ---------------- 提取 JSON ----------------
def _balanced(text: str, open_ch: str, close_ch: str) -> str:
    start = text.find(open_ch)
    if start < 0:
        return ""
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _as_items(obj: Any) -> List[Dict[str, Any]]:
    if isinstance(obj, list):
        if obj and all(isinstance(x, list) for x in obj):
            flat: List[Any] = []
            for sub in obj:
                flat.extend(sub)
            obj = flat
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in LIST_KEYS:
            value = obj.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        if any(k in obj for k in ("stem", "question", "题干")):
            return [obj]
    return []


def _from_text(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        raise ParseError("返回内容为空")
    # 1) 整段就是 JSON
    try:
        items = _as_items(json.loads(text))
        if items:
            return items
    except json.JSONDecodeError:
        pass
    # 2) 截取第一个完整的 JSON 数组 / 对象
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        block = _balanced(text, open_ch, close_ch)
        if not block:
            continue
        try:
            items = _as_items(json.loads(block))
            if items:
                return items
        except json.JSONDecodeError:
            continue
    # 3) 逐行 JSON（JSONL）
    items = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    if items:
        return items
    raise ParseError(f"没有找到题目 JSON 数组，开头是：{text[:120]}")


def parse_questions(text: str) -> List[Dict[str, Any]]:
    """从模型返回文本中提取题目列表；优先尝试代码块里最长的一段。"""
    text = (text or "").strip()
    blocks = sorted(FENCE_RE.findall(text), key=len, reverse=True)
    candidates = blocks[:3] + [text]
    last_err: Optional[Exception] = None
    for cand in candidates:
        if not cand.strip():
            continue
        try:
            return _from_text(cand)
        except ParseError as exc:
            last_err = exc
    raise ParseError(str(last_err) if last_err else "模型没有返回题目")


# ---------------- 规范字段 ----------------
def _clean(value: Any) -> str:
    text = str(value or "").replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip("*").strip()


def _clean_option(value: Any) -> str:
    text = _clean(value)
    text = PREFIX_RE.sub("", text)
    return text.strip()


def _answer_index(raw: Any, options: List[str]) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        num = int(raw)
        if 0 <= num <= 3:
            return num
        if 1 <= num <= 4:
            return num - 1
        return None
    text = _clean(raw)
    if not text:
        return None
    m = re.match(r"^(?:答案|正确答案|answer|correct)\s*[:：]?\s*[（(]?([A-Da-d])",
                 text, re.I)
    if not m:
        m = re.match(r"^[（(]?([A-Da-d])[)）]?(?:[.、．:：]|\s|$)", text)
    if m:
        return "ABCD".index(m.group(1).upper())
    # 直接给了选项原文
    target = re.sub(r"[\s，。、；：,.!?！？\"'“”‘’]", "", text)
    for idx, opt in enumerate(options):
        key = re.sub(r"[\s，。、；：,.!?！？\"'“”‘’]", "", opt)
        if key and key == target:
            return idx
    return None


def _difficulty(raw: Any, default: int = 2) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        return max(1, min(4, int(round(raw))))
    text = _clean(raw)
    if not text:
        return default
    m = re.search(r"[1-4]", text)
    if m:
        return int(m.group(0))
    for label in ("较难", "容易", "简单", "基础", "中等", "困难", "挑战"):
        if label in text:
            return DIFF_CN[label]
    if "难" in text:
        return 4
    return default


def _bloom(raw: Any, fallback: str = "理解") -> str:
    text = _clean(raw)
    if not text:
        return fallback
    if text in BLOOM_CN:
        return text
    low = text.lower()
    if low in BLOOM_EN:
        return BLOOM_EN[low]
    for cn in BLOOM_CN:
        if cn in text:
            return cn
    return fallback


def question_id(topic_id: str, stem: str) -> str:
    key = re.sub(r"[\W_]+", "", (stem or "").lower())
    raw = f"{topic_id}|{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _grade_text(vol: Dict[str, Any]) -> str:
    grade = store._grade_label(vol.get("grade", ""))
    term = store.TERM_NAMES.get(vol.get("term", ""), "")
    return grade + term


def normalize_question(item: Dict[str, Any], kp: Dict[str, Any],
                       vol: Dict[str, Any], default_difficulty: int = 2,
                       provider: str = "", model: str = "",
                       task_id: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
    """校验并规范单题；不合格返回 (None, 原因)。"""
    stem = _clean(item.get("stem") or item.get("question") or item.get("题干"))
    if not stem:
        return None, "题干为空"
    if len(stem) < 4:
        return None, f"题干过短：{stem}"
    if len(stem) > 1000:
        return None, "题干超长"

    raw_options = item.get("options") or item.get("选项")
    if not raw_options:
        raw_options = [item.get(k) for k in ("option_a", "option_b", "option_c", "option_d")]
    if isinstance(raw_options, str):
        raw_options = re.split(r"\s*[A-D][.、．:：]\s*", raw_options)
    if isinstance(raw_options, dict):
        # 少数模型返回 {"A": "...", "B": "..."}
        raw_options = [raw_options[k] for k in sorted(raw_options)]
    if not isinstance(raw_options, (list, tuple)):
        return None, "选项缺失"
    options = [_clean_option(o) for o in raw_options if _clean_option(o)]
    if len(options) < 4:
        return None, f"选项少于 4 个（{len(options)}）"
    if len(options) > 4:
        options = options[:4]
    if len(set(options)) != 4:
        return None, "选项重复"
    if any(len(o) > 200 for o in options):
        return None, "选项超长"

    idx = _answer_index(item.get("answer") if item.get("answer") is not None
                        else (item.get("答案") or item.get("correct")), options)
    if idx is None or not 0 <= idx < 4:
        return None, "答案无法识别"

    explanation = _clean(item.get("explanation") or item.get("解析")
                         or item.get("analysis"))
    if not explanation:
        return None, "缺少解析"
    if len(explanation) > 2000:
        explanation = explanation[:2000]

    difficulty = _difficulty(item.get("difficulty") or item.get("难度"),
                             default_difficulty)
    bloom = _bloom(item.get("bloom") or item.get("认知层级"), kp.get("bloom", "理解"))
    now = dt.datetime.now().isoformat(timespec="seconds")
    qid = question_id(kp.get("id", ""), stem)
    record = {
        "id": qid,
        "topic_id": kp.get("id", ""),
        "topic_name": kp.get("name", ""),
        "volume_id": vol.get("volume_id", ""),
        "volume_title": vol.get("title", ""),
        "stage": vol.get("stage", ""),
        "subject": vol.get("subject", ""),
        "grade": vol.get("grade", ""),
        "grade_label": _grade_text(vol),
        "chapter": kp.get("unit", ""),
        "domain": kp.get("domain", ""),
        "stem": stem,
        "options": options,
        "answer": "ABCD"[idx],
        "answer_text": options[idx],
        "explanation": explanation,
        "difficulty": difficulty,
        "bloom": bloom,
        "type": "single_choice",
        "source": "llm",
        "provider": provider,
        "model": model,
        "task_id": task_id,
        "created_at": now,
    }
    return record, ""


def normalize_batch(items: List[Dict[str, Any]], kp: Dict[str, Any],
                    vol: Dict[str, Any], default_difficulty: int = 2,
                    provider: str = "", model: str = "",
                    task_id: str = "") -> Tuple[List[Dict[str, Any]], List[str]]:
    """批量规范 + 批内去重；返回 (合格题目, 丢弃原因列表)。"""
    records: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen = set()
    for item in items:
        record, err = normalize_question(item, kp, vol, default_difficulty,
                                         provider, model, task_id)
        if record is None:
            errors.append(err)
            continue
        if record["id"] in seen:
            errors.append("题干重复")
            continue
        seen.add(record["id"])
        records.append(record)
    return records, errors
