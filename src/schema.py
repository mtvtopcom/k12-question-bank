#!/usr/bin/env python3
"""统一题目 Schema 与校验。零第三方依赖。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


def make_id(topic_id: str, variant_index: int, seed: int, source: str) -> str:
    """确定性 ID：同样的输入永远得到同样的 id，保证幂等。"""
    raw = f"{topic_id}|{variant_index}|{seed}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@dataclass
class Quality:
    dedup_hash: str = ""
    qa_passed: bool = True
    qa_notes: List[str] = field(default_factory=list)
    cross_verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Question:
    id: str
    topic_id: str
    grade: str
    subject: str
    topic: str
    difficulty: int                 # 1基础 2中等 3提高 4挑战
    bloom: str                      # 记忆/理解/应用/分析
    type: str                       # single_choice / multi_choice
    stem: str
    options: List[str]
    answer: str                     # "A"/"B"/"C"/"D"
    explanation: str
    source: str                     # template / llm
    generator_version: str = "v1.0"
    chapter: str = ""
    quality: Quality = field(default_factory=Quality)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
