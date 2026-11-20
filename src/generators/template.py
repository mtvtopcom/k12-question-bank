#!/usr/bin/env python3
"""模板引擎：规则类题目的确定性生成器。

题型：
  数学 - arithmetic(四则) / fraction(分数) / geometry(几何) / percentage(百分数) / equation(方程)
  英语 - vocabulary(词汇) / grammar(语法)
  语文 - pinyin(拼音) / idiom(成语) / characters(字词)
  科学 - science_basic(科学常识)

每道题由 (topic_id, 题型, variant_index, seed) 决定，完全可复现。
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Dict, List

from schema import Question, make_id
from generators.base import BaseGenerator

VERSION = "v1.0"


def _rng_for(*parts) -> random.Random:
    key = "|".join(str(p) for p in parts)
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(h)


class TemplateGenerator(BaseGenerator):
    def __init__(self, seed: int, enabled: Dict[str, List[str]] | None = None):
        super().__init__(seed)
        self.enabled = enabled or {}

    def generate(self, topic: Dict[str, Any], n: int) -> List[Question]:
        subject = topic["subject"]
        kinds = self._match_kinds(topic, n)
        out: List[Question] = []
        for i in range(n):
            kind = kinds[i % len(kinds)]
            maker = getattr(self, f"_gen_{kind}", None)
            if maker is None:
                continue
            q = maker(topic, i, _rng_for(topic["topic_id"], kind, i, self.seed))
            if q is not None:
                out.append(q)
        return out

    def _match_kinds(self, topic: Dict[str, Any], n: int) -> List[str]:
        """按 topic 关键词优先匹配题型，让题目贴合知识点。未命中则用科目默认集合。"""
        subject = topic["subject"]
        defaults = self.enabled.get(subject, self._default_kinds(subject))
        name = topic.get("topic", "")
        rules = [
            # 注意顺序：percentage 要在 fraction 前，因为"百分数"包含"分数"
            ("percentage", ["百分数", "百分比", "折扣", "利率", "成数"]),
            ("fraction", ["分数", "约分", "通分"]),
            ("geometry", ["圆", "面积", "周长", "图形", "几何", "长方体", "正方体", "三角形", "扇形", "体积"]),
            ("equation", ["方程", "未知数", "等式", "解方程"]),
            ("arithmetic", ["加", "减", "乘", "除", "计算", "四则", "运算", "口算"]),
            ("vocabulary", ["单词", "词汇", "词", "vocabulary"]),
            ("grammar", ["语法", "时态", "句型", "grammar"]),
            ("pinyin", ["拼音", "读音", "音节"]),
            ("idiom", ["成语", "词语理解", "古诗"]),
            ("characters", ["字词", "汉字", "生字", "笔画", "偏旁"]),
        ]
        for kind, keywords in rules:
            if any(kw in name for kw in keywords) and kind in defaults + self._default_kinds(subject):
                # 命中知识点题型，主题型占多数，保留少量其他题型增加多样性
                return [kind] * max(1, n - 2) + [k for k in defaults if k != kind][:2]
        return defaults

    def _default_kinds(self, subject: str) -> List[str]:
        return {
            "数学": ["arithmetic", "fraction", "geometry", "percentage", "equation"],
            "英语": ["vocabulary", "grammar"],
            "语文": ["pinyin", "idiom", "characters"],
            "科学": ["science_basic"],
        }.get(subject, ["arithmetic"])

    def _base(self, topic: Dict[str, Any], i: int, kind: str) -> Dict[str, Any]:
        return {
            "topic_id": topic["topic_id"],
            "grade": topic["grade"],
            "subject": topic["subject"],
            "topic": topic["topic"],
            "chapter": topic.get("chapter", ""),
            "difficulty": 1 + (i % 3),
            "bloom": ["记忆", "理解", "应用"][i % 3],
            "type": "single_choice",
            "source": "template",
            "generator_version": VERSION,
        }

    def _make(self, topic, i, kind, stem, correct, distractors, explanation, rng, bloom=None, difficulty=None):
        options, answer = self.shuffle_options(stem, correct, distractors, rng)
        base = self._base(topic, i, kind)
        if bloom:
            base["bloom"] = bloom
        if difficulty:
            base["difficulty"] = difficulty
        return Question(
            id=make_id(topic["topic_id"], i, self.seed, f"tpl_{kind}"),
            stem=stem, options=options, answer=answer, explanation=explanation,
            **base,
        )

    # ---------------- 数学 ----------------
    def _gen_arithmetic(self, topic, i, rng):
        lo, hi = self._num_range(topic["grade"])
        # 高年级扩大取值上限，增加组合空间
        span = max(hi - lo, 10)
        a, b = rng.randint(lo, hi + span), rng.randint(lo, hi + span)
        mode = rng.choice(["add", "sub", "mul", "word"])
        if mode == "word":
            # 情境应用题：同一运算套不同生活场景
            scene, unit = rng.choice([
                ("小明有{a}个苹果，又买了{b}个，现在一共有", "个苹果"),
                ("书架上有{a}本书，又放上{b}本，共有", "本书"),
                ("商店运来{a}箱牛奶，下午又运来{b}箱，全天共运来", "箱"),
                ("停车场原有{a}辆车，开来了{b}辆，现有", "辆车"),
            ])
            c = a + b
            stem = scene.format(a=a, b=b) + "多少" + unit + "？"
            expl = f"{a} + {b} = {c}"
            dis = [str(c + rng.randint(1, 9)), str(a), str(b), str(c + rng.randint(10, 20))]
        elif mode == "add":
            c = a + b; stem = f"{a} + {b} = ?"; expl = f"{a} + {b} = {c}"
            dis = [str(c + rng.randint(1, 9)), str(max(0, c - rng.randint(1, 9))), str(a - b if a > b else a + b)]
        elif mode == "sub":
            a, b = max(a, b), min(a, b)
            c = a - b; stem = f"{a} - {b} = ?"; expl = f"{a} - {b} = {c}"
            dis = [str(c + rng.randint(1, 9)), str(max(0, c - rng.randint(1, 9))), str(a + b)]
        else:
            a, b = rng.randint(2, 12), rng.randint(2, 12)
            c = a * b; stem = f"{a} × {b} = ?"; expl = f"{a} × {b} = {c}"
            dis = [str(a * b + a), str(a * b - a), str(a + b), str(a * (b + 1))]
        return self._make(topic, i, "arithmetic", stem, str(c), dis, expl, rng)

    def _gen_fraction(self, topic, i, rng):
        from fractions import Fraction
        a, b = rng.randint(1, 9), rng.randint(2, 9)
        c, d = rng.randint(1, 9), rng.randint(2, 9)
        f1, f2 = Fraction(a, b), Fraction(c, d)
        if rng.random() < 0.5:
            res = f1 + f2; op = "+"
        else:
            res = f1 - f2; op = "-"
        stem = f"计算 {a}/{b} {op} {c}/{d} 的结果是？"
        correct = self._frac_str(res)
        dis = [self._frac_str(res + Fraction(1, 1)), self._frac_str(abs(res - Fraction(1, 2)) or Fraction(1, 3)),
               self._frac_str(res + Fraction(1, 2)), f"{a + c}/{b + d}"]
        expl = f"{a}/{b} {op} {c}/{d} = {correct}"
        return self._make(topic, i, "fraction", stem, correct, dis, expl, rng)

    def _gen_geometry(self, topic, i, rng):
        kind = rng.choice(["rect", "circle", "triangle"])
        if kind == "rect":
            w, h = rng.randint(3, 20), rng.randint(3, 20)
            area = w * h; stem = f"一个长方形长 {w} 厘米、宽 {h} 厘米，面积是多少平方厘米？"
            correct = f"{area}平方厘米"
            dis = [f"{2*(w+h)}平方厘米", f"{w+h}平方厘米", f"{area*2}平方厘米"]
            expl = f"长方形面积 = 长 × 宽 = {w} × {h} = {area} 平方厘米"
        elif kind == "circle":
            r = rng.randint(2, 10)
            area = 3.14 * r * r; stem = f"一个圆半径 {r} 厘米，面积约是多少平方厘米？（π取3.14）"
            correct = f"{round(area,2)}平方厘米"
            dis = [f"{round(2*3.14*r,2)}平方厘米", f"{round(3.14*r,2)}平方厘米", f"{round(r*r,2)}平方厘米"]
            expl = f"圆面积 = πr² = 3.14 × {r}² = {round(area,2)} 平方厘米"
        else:
            base, height = rng.randint(3, 20), rng.randint(3, 20)
            area = base * height // 2; stem = f"一个三角形底 {base} 厘米、高 {height} 厘米，面积是多少平方厘米？"
            correct = f"{area}平方厘米"
            dis = [f"{base*height}平方厘米", f"{base+height}平方厘米", f"{area*2}平方厘米"]
            expl = f"三角形面积 = 底 × 高 ÷ 2 = {base} × {height} ÷ 2 = {area} 平方厘米"
        return self._make(topic, i, "geometry", stem, correct, dis, expl, rng)

    def _gen_percentage(self, topic, i, rng):
        total = rng.choice([100, 200, 250, 400, 500])
        pct = rng.choice([10, 20, 25, 30, 40, 50, 60, 75, 80])
        val = total * pct // 100
        stem = f"{total} 的 {pct}% 是多少？"
        correct = str(val)
        dis = [str(pct), str(total - val), str(val * 2), str(total // pct if pct else 1)]
        expl = f"{total} × {pct}% = {total} × {pct/100} = {val}"
        return self._make(topic, i, "percentage", stem, correct, dis, expl, rng)

    def _gen_equation(self, topic, i, rng):
        x = rng.randint(2, 15); a = rng.randint(2, 9); b = rng.randint(1, 20)
        stem = f"解方程：{a}x + {b} = {a*x + b}，x 等于多少？"
        correct = str(x)
        dis = [str(x + 1), str(x - 1), str(a * x + b), str((a * x + b) // a + 1)]
        expl = f"{a}x = {a*x + b} - {b} = {a*x}，所以 x = {x}"
        return self._make(topic, i, "equation", stem, correct, dis, expl, rng)

    # ---------------- 英语 ----------------
    def _gen_vocabulary(self, topic, i, rng):
        bank = [
            ("apple", "苹果"), ("book", "书"), ("school", "学校"), ("teacher", "老师"),
            ("water", "水"), ("happy", "快乐的"), ("student", "学生"), ("friend", "朋友"),
            ("family", "家庭"), ("morning", "早晨"), ("library", "图书馆"), ("beautiful", "美丽的"),
            ("run", "跑"), ("eat", "吃"), ("read", "读"), ("write", "写"),
        ]
        word, meaning = rng.choice(bank)
        others = [m for _, m in bank if m != meaning]
        rng.shuffle(others)
        stem = f"英语单词 \"{word}\" 的中文意思是？"
        return self._make(topic, i, "vocabulary", stem, meaning, others[:3], f"{word} 的意思是「{meaning}」", rng)

    def _gen_grammar(self, topic, i, rng):
        items = [
            ("I ___ a student.", "am", ["is", "are", "be"], "主语 I 搭配 be 动词 am"),
            ("She ___ to school every day.", "goes", ["go", "going", "gone"], "第三人称单数一般现在时动词加 -es"),
            ("There ___ some water in the cup.", "is", ["are", "have", "has"], "water 不可数，用 is"),
            ("They ___ football now.", "are playing", ["play", "plays", "played"], "now 提示现在进行时"),
            ("I have ___ apples than you.", "more", ["many", "much", "most"], "than 提示比较级 more"),
        ]
        stem, correct, dis, expl = rng.choice(items)
        return self._make(topic, i, "grammar", stem, correct, dis, expl, rng)

    # ---------------- 语文 ----------------
    def _gen_pinyin(self, topic, i, rng):
        items = [
            ("苹果", "píng guǒ", ["pín guǒ", "píng guó", "pìng guǒ"]),
            ("学校", "xué xiào", ["xué xiāo", "xué xiào", "xuè xiào"]),
            ("老师", "lǎo shī", ["lǎo shī", "lāo shī", "lǎo shí"]),
            ("美丽", "měi lì", ["méi lì", "měi lì", "mèi lì"]),
            ("朋友", "péng you", ["péng yǒu", "pén you", "pèng you"]),
        ]
        word, correct, dis = rng.choice(items)
        stem = f"词语「{word}」的正确读音是？"
        # 去重干扰项中的正确项
        dis = [d for d in dis if d != correct]
        while len(dis) < 3:
            dis.append(correct + "?" if len(dis) < 1 else "—")
        return self._make(topic, i, "pinyin", stem, correct, dis, f"「{word}」读作 {correct}", rng)

    def _gen_idiom(self, topic, i, rng):
        items = [
            ("画蛇添足", "比喻多此一举", ["比喻坚持不懈", "比喻画技高超", "比喻胆子很大"]),
            ("守株待兔", "比喻死守经验不知变通", ["比喻勤劳致富", "比喻等待机会", "比喻种田有趣"]),
            ("亡羊补牢", "比喻出了问题及时补救", ["比喻为时已晚", "比喻丢失东西", "比喻养羊很多"]),
            ("掩耳盗铃", "比喻自欺欺人", ["比喻动作敏捷", "比喻偷铃技术", "比喻听力不好"]),
        ]
        idiom, correct, dis = rng.choice(items)
        stem = f"成语「{idiom}」的意思是？"
        return self._make(topic, i, "idiom", stem, correct, dis, f"「{idiom}」：{correct}", rng)

    def _gen_characters(self, topic, i, rng):
        items = [
            ("「休」字由哪两个部分组成？", "人、木", ["人、本", "木、木", "人、未"]),
            ("「明」字的意思是？", "明亮", ["明天才有的字", "动物的名字", "一种颜色"]),
            ("「森」字由几个「木」组成？", "三个", ["两个", "四个", "一个"]),
            ("「河」字的偏旁是？", "三点水", ["木字旁", "提手旁", "言字旁"]),
        ]
        stem, correct, dis = rng.choice(items)
        return self._make(topic, i, "characters", stem, correct, dis, f"正确答案是「{correct}」", rng)

    # ---------------- 科学 ----------------
    def _gen_science_basic(self, topic, i, rng):
        items = [
            ("水的化学式是？", "H₂O", ["CO₂", "O₂", "H₂"]),
            ("地球围绕什么公转？", "太阳", ["月球", "火星", "地轴"]),
            ("人体最大的器官是？", "皮肤", ["心脏", "肺", "肝脏"]),
            ("植物进行光合作用需要什么气体？", "二氧化碳", ["氧气", "氮气", "氢气"]),
            ("声音在哪种介质中传播最快？", "固体", ["液体", "气体", "真空"]),
        ]
        stem, correct, dis = rng.choice(items)
        return self._make(topic, i, "science_basic", stem, correct, dis, f"正确答案是「{correct}」", rng)

    # ---------------- 工具 ----------------
    @staticmethod
    def _frac_str(f) -> str:
        from fractions import Fraction
        if isinstance(f, int):
            return str(f)
        return f"{f.numerator}/{f.denominator}"

    @staticmethod
    def _num_range(grade: str):
        if any(k in grade for k in ("一年级", "幼儿园")):
            return (1, 20)
        if "二年级" in grade:
            return (10, 99)
        if "三年级" in grade:
            return (50, 999)
        return (100, 9999)
