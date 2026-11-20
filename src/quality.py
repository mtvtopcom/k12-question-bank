#!/usr/bin/env python3
"""质量门禁：结构校验 + 答案一致性 + 语义去重（字符 n-gram Jaccard）。

无第三方依赖。去重采用 SimHash 思路的简化版：字符 3-gram 集合 + Jaccard 相似度。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from schema import Question


def normalize(text: str) -> str:
    """归一化：去空白、标点、统一大小写，用于去重比对。"""
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[，。！？、；：\"'“”‘’（）()【】\[\].,!?;:·\-\—]", "", text)
    return text.lower()


def ngrams(text: str, n: int = 3) -> set:
    t = normalize(text)
    if len(t) < n:
        return {t} if t else set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def dedup_hash(stem: str) -> str:
    return hashlib.md5(normalize(stem).encode("utf-8")).hexdigest()[:16]


class QualityGate:
    def __init__(self, cfg: Dict[str, Any]):
        q = cfg.get("quality", {})
        self.min_options = int(q.get("min_options", 4))
        self.max_options = int(q.get("max_options", 4))
        self.require_explanation = bool(q.get("require_explanation", True))
        self.min_explanation_chars = int(q.get("min_explanation_chars", 8))
        self.threshold = float(q.get("dedup_jaccard_threshold", 0.85))
        self.check_answer = bool(q.get("check_answer_in_options", True))
        self._seen_grams: List[Tuple[str, set]] = []  # (id, grams)
        self._seen_hashes: Dict[str, str] = {}

    def validate_structure(self, q: Question) -> Tuple[bool, List[str]]:
        notes: List[str] = []
        if not q.stem or len(q.stem) < 6:
            notes.append("题干过短或为空")
        if not (self.min_options <= len(q.options) <= self.max_options):
            notes.append(f"选项数 {len(q.options)} 不在 [{self.min_options},{self.max_options}]")
        if len(set(q.options)) != len(q.options):
            notes.append("选项存在重复")
        if self.check_answer:
            if q.answer not in ("A", "B", "C", "D"):
                notes.append(f"答案格式异常: {q.answer}")
            else:
                idx = ord(q.answer) - ord("A")
                if idx >= len(q.options):
                    notes.append("答案索引越界")
        if self.require_explanation and len(q.explanation) < self.min_explanation_chars:
            notes.append("解析过短或为空")
        if q.difficulty not in (1, 2, 3, 4):
            notes.append(f"难度值异常: {q.difficulty}")
        return (len(notes) == 0), notes

    def is_duplicate(self, q: Question) -> Tuple[bool, str]:
        h = dedup_hash(q.stem)
        if h in self._seen_hashes:
            return True, self._seen_hashes[h]
        grams = ngrams(q.stem)
        for prev_id, prev_grams in self._seen_grams:
            sim = jaccard(grams, prev_grams)
            if sim >= self.threshold:
                return True, prev_id
        self._seen_hashes[h] = q.id
        self._seen_grams.append((q.id, grams))
        return False, ""

    def load_existing(self, questions: List[Dict[str, Any]]) -> None:
        """从库里加载已有题干，避免跨批次重复。"""
        for d in questions:
            stem = d.get("stem", "")
            qid = d.get("id", "")
            h = dedup_hash(stem)
            self._seen_hashes[h] = qid
            self._seen_grams.append((qid, ngrams(stem)))

    def check(self, q: Question) -> Tuple[bool, List[str]]:
        ok, notes = self.validate_structure(q)
        if not ok:
            return False, notes
        dup, prev = self.is_duplicate(q)
        if dup:
            q.quality.qa_passed = False
            q.quality.qa_notes.append(f"与 {prev} 高度重复")
            return False, q.quality.qa_notes
        q.quality.dedup_hash = dedup_hash(q.stem)
        q.quality.qa_passed = True
        return True, []
