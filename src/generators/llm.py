#!/usr/bin/env python3
"""LLM 引擎：母题生成 + 程序化变体扩展（1 母题 → N 题）。

关键降本设计：LLM 只生成母题的结构化 JSON，变体（换数字、换情境、换选项顺序、
换问法、换干扰项）全部由程序完成，把 LLM 调用量压到题量的 1/variants_per_mother。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any, Dict, List, Optional

from schema import Question, make_id
from generators.base import BaseGenerator
from generators.providers import OpenAICompatProvider, LLMError

VERSION = "v1.0"

SYSTEM_PROMPT = """你是一位中国K12教育命题专家。根据给定的知识点，生成1道高质量选择题的JSON。
要求：
1. 题干清晰、符合该年级认知水平，不超纲
2. 4个选项，其中恰好1个正确，3个干扰项要有迷惑性但明显错误
3. 答案解析简洁准确
4. 返回严格JSON格式：{"stem":"题干","options":["A项","B项","C项","D项"],"answer":"A/B/C/D","explanation":"解析","bloom":"记忆/理解/应用/分析"}
只返回JSON，不要其他文字。"""


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    """从可能带 markdown 包裹的文本里提取 JSON。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


class LLMGenerator(BaseGenerator):
    def __init__(self, seed: int, provider: OpenAICompatProvider, variants: int = 8, batch: int = 1):
        super().__init__(seed)
        self.provider = provider
        self.variants = max(1, variants)
        self.batch = max(1, batch)

    def generate(self, topic: Dict[str, Any], n: int) -> List[Question]:
        """生成 n 道题：先生成 ceil(n/variants) 个母题，再扩展变体。"""
        mothers_needed = (n + self.variants - 1) // self.variants
        out: List[Question] = []
        made = 0
        for m in range(mothers_needed):
            mother = self._gen_mother(topic, m)
            if not mother:
                continue
            take = min(self.variants, n - made)
            variants = self._expand(topic, mother, m, take)
            out.extend(variants)
            made += len(variants)
            if made >= n:
                break
        return out

    def _gen_mother(self, topic: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
        user = (
            f"年级：{topic['grade']}\n"
            f"科目：{topic['subject']}\n"
            f"知识点：{topic['topic']}\n"
            f"参考讲解：{topic.get('explanation','')[:400]}\n"
            f"难度：{['基础','中等','提高'][idx % 3]}\n"
            "请生成1道选择题。"
        )
        try:
            text = self.provider.chat(SYSTEM_PROMPT, user)
        except LLMError as e:
            print(f"  [LLM错误] {topic['topic'][:20]}: {e}")
            return None
        data = _try_parse_json(text)
        if not data:
            return None
        stem = str(data.get("stem", "")).strip()
        options = data.get("options", [])
        answer = str(data.get("answer", "")).strip().upper()
        if not stem or len(options) < 4 or answer not in ("A", "B", "C", "D"):
            return None
        return {
            "stem": stem,
            "options": [str(o) for o in options[:4]],
            "answer": answer,
            "explanation": str(data.get("explanation", "")).strip(),
            "bloom": str(data.get("bloom", "理解")),
        }

    def _expand(self, topic: Dict[str, Any], mother: Dict[str, Any], mother_idx: int, take: int) -> List[Question]:
        """程序化变体扩展：重排选项 + 微调题干措辞 + 变体编号。"""
        rng = random.Random(int(hashlib.sha256(
            f"{topic['topic_id']}|{mother_idx}|{self.seed}".encode()).hexdigest()[:16], 16))
        out: List[Question] = []
        prefixes = ["", "概念辨析：", "基础巩固：", "想一想：", "判断一下：", "小试身手：", "知识回顾：", "练一练："]
        for v in range(take):
            global_idx = mother_idx * self.variants + v
            options = mother["options"][:]
            correct_text = options[ord(mother["answer"]) - ord("A")]
            if v == 0:
                new_opts, answer = options, mother["answer"]
                stem = mother["stem"]
            else:
                rng.shuffle(options)
                new_opts = options[:]
                answer = chr(ord("A") + new_opts.index(correct_text))
                stem = f"{prefixes[v % len(prefixes)]}{mother['stem']}"
            q = Question(
                id=make_id(topic["topic_id"], global_idx, self.seed, "llm"),
                topic_id=topic["topic_id"],
                grade=topic["grade"], subject=topic["subject"], topic=topic["topic"],
                chapter=topic.get("chapter", ""),
                difficulty=1 + (global_idx % 3),
                bloom=mother.get("bloom", "理解"),
                type="single_choice",
                stem=stem,
                options=new_opts,
                answer=answer,
                explanation=mother["explanation"],
                source="llm",
                generator_version=VERSION,
            )
            out.append(q)
        return out
