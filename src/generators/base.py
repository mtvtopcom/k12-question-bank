#!/usr/bin/env python3
"""生成器基类。"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List

from schema import Question


class BaseGenerator(ABC):
    def __init__(self, seed: int):
        self.seed = seed

    @abstractmethod
    def generate(self, topic: Dict[str, Any], n: int) -> List[Question]:
        """为单个知识点生成 n 道题。"""
        raise NotImplementedError

    @staticmethod
    def shuffle_options(stem: str, correct: str, distractors: List[str], rng: random.Random):
        """把正确答案和最多3个干扰项打乱，返回 (options, answer_letter)。

        先去重并限制干扰项数量，再洗牌，避免截断后答案丢失。
        """
        clean: List[str] = []
        for d in distractors:
            if d and d != correct and d not in clean:
                clean.append(d)
            if len(clean) >= 3:
                break
        opts = [correct] + clean
        rng.shuffle(opts)
        letters = "ABCD"
        answer = letters[opts.index(correct)]
        return opts, answer
