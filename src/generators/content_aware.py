#!/usr/bin/env python3
"""内容感知型模板生成器（考查知识内容，而非知识点档案）。

与旧 TemplateGenerator 的根本区别：不从硬编码词表出题，而是读取每个知识点
自身的内容字段来命题。v2.1 起去掉了「领域归类 / 目标匹配 / 前置知识」等
元数据题型——那些题把知识点名称当题干、考它的档案字段，不是考查学习内容。
现在的题：题干围绕学习目标（考核要求）设置情境，选项全部是**内容陈述**
（具体做法、概念事实、典型误区、关键术语），考的是知识点本身。

数据来源（knowledge_points.jsonl 的实际字段）：
    explanation = "<概念讲解>\n易错点：<错误1>；<错误2>……"
    objective   = 学习目标（考核要求），用作题干情境
    concept     = 概念讲解中的具体事实/做法，用作正确项
    mistakes    = 典型误区，用作错误项或考查对象

题型策略（答案均可从知识点自身判定，不依赖外部事实）：
    fact_choice   要点判断：concept 的具体做法为正确项，mistakes + 同学科其他要点为干扰项
    mistake_spot  误区识别：mistake 为答案，concept 要点 + 同学科其他要点为干扰项
    term_cloze    术语填空：把 concept 中含「术语」的句子挖空，术语为答案
    math_numeric  数学参数化：委托旧引擎的数值题，提供无限不重复题干（真正考运算能力）

每种策略配多套「句子结构不同」的问法模板，使题干差异足够大，
能通过 quality.py 的字符 3-gram Jaccard 去重门禁。

所有题目由 (topic_id, 策略, variant, seed) 决定，完全可复现。
"""
from __future__ import annotations

import hashlib
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from schema import Question, make_id
from generators.base import BaseGenerator
from generators.template import TemplateGenerator

VERSION = "v2.0-content-aware"
MISTAKE_MARK = "\n易错点："

# 题干前缀：与既有 LLM 变体扩展一致，作为最轻量的问法变化
PREFIXES = ["", "基础巩固：", "概念辨析：", "想一想：", "练一练：",
            "知识回顾：", "判断一下：", "小试身手："]

# 每种策略多套问法模板，句子结构差异大（降低互相的 n-gram 相似度）
TEMPLATES: Dict[str, List[str]] = {
    # 正确项是 concept 里的具体做法/事实，干扰项含真实误区——考「怎么做才对」
    "fact_choice": [
        "围绕学习目标“{obj}”，下列哪种做法是正确的？",
        "为了做到“{obj}”，下面哪一项做法恰当？",
        "在学习「{topic}」时，哪种处理方式符合要求？",
        "要达成“{obj}”，应当采取下面哪一做法？",
        "关于「{topic}」，下列做法中合理的是？",
        "按照“{obj}”的要求，应选择哪一项？",
    ],
    # 正确项是典型误区——考「哪种做法是错的/要避免」
    "mistake_spot": [
        "学习「{topic}」时，下面哪种做法容易出错？",
        "要达成“{obj}”，下列哪一项是需要避免的？",
        "关于「{topic}」，哪种情况属于常见误区？",
        "在「{topic}」的学习中，哪一做法不恰当？",
        "对照“{obj}”，下面哪项做法会偏离目标？",
        "「{topic}」学习中，老师最应避免的是？",
    ],
    # 把 concept 中含「术语」的句子挖空——考核心要领本身
    "term_cloze": [
        "“{anchor}”——空缺处应填入下列哪个要领？",
        "下面哪个术语最适合填入：“{anchor}”？",
        "“{anchor}”这句话中的关键要领是？",
        "补全这句话：“{anchor}”，应选？",
        "“{anchor}”——横线处对应哪个概念？",
        "要使这句话成立：“{anchor}”，关键术语是？",
    ],
}


def _rng_for(*parts) -> random.Random:
    key = "|".join(str(p) for p in parts)
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(h)


def split_explanation(text: str) -> Tuple[str, List[str]]:
    """把 explanation 拆成 (concept, mistakes 列表)。"""
    parts = (text or "").split(MISTAKE_MARK, 1)
    concept = parts[0].strip()
    mistakes: List[str] = []
    if len(parts) > 1:
        for seg in re.split(r"[；;]", parts[1]):
            seg = seg.strip(" 。，、：:")
            if len(seg) >= 3:
                mistakes.append(seg)
    return concept, mistakes


def clauses(text: str, min_len: int = 6) -> List[str]:
    """把讲解文本切成可作题干锚点的短句。"""
    out: List[str] = []
    for seg in re.split(r"[。；;！？\n]", text or ""):
        seg = seg.strip(" ，、：:！？。；;")
        if len(seg) >= min_len:
            out.append(seg)
    return out


def quoted_terms(text: str) -> List[str]:
    """抽取「」中的关键术语。"""
    out: List[str] = []
    for t in re.findall(r"「([^」]{2,24})」", text or ""):
        if t not in out:
            out.append(t)
    return out


def _clip(text: str, limit: int = 72) -> str:
    text = re.sub(r"\s+", "", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class ContentAwareGenerator(BaseGenerator):
    """读取知识点内容命题的确定性生成器。"""

    def __init__(self, seed: int, corpus: Optional[List[Dict[str, Any]]] = None,
                 math_numeric: bool = True):
        super().__init__(seed)
        self.math_numeric = math_numeric
        self._legacy = TemplateGenerator(seed)
        self._corpus: List[Dict[str, Any]] = []
        self._titles_by_subject: Dict[str, List[str]] = {}
        self._objectives: List[str] = []
        self._objectives_by_subject: Dict[str, List[str]] = {}
        self._domains: List[str] = []
        self._themes: List[str] = []
        self._terms: List[str] = []
        self._terms_by_subject: Dict[str, List[str]] = {}
        self._title_by_id: Dict[str, str] = {}
        self._facts_by_subject: Dict[str, List[str]] = {}   # 同学科 concept 事实，做同质干扰项
        self._facts_by_domain: Dict[Tuple[str, str], List[str]] = {}  # (学科, 领域) → 事实，优先同领域内聚
        if corpus:
            self.set_corpus(corpus)

    # ---------- 语料索引 ----------
    def set_corpus(self, topics: List[Dict[str, Any]]) -> None:
        """建立全库索引，供跨知识点抽干扰项。可重复调用。"""
        self._corpus = list(topics)
        titles: Dict[str, List[str]] = {}
        objectives: List[str] = []
        obj_by_subj: Dict[str, List[str]] = {}
        domains, themes, terms = [], [], []
        terms_by_subj: Dict[str, List[str]] = {}
        facts_by_subj: Dict[str, List[str]] = {}
        facts_by_domain: Dict[Tuple[str, str], List[str]] = {}
        title_by_id: Dict[str, str] = {}
        for t in topics:
            subj = t.get("subject", "")
            name = t.get("topic", "")
            if name:
                title_by_id[t.get("topic_id", "")] = name
                if name not in titles.setdefault(subj, []):
                    titles[subj].append(name)
            obj = t.get("objective", "")
            if obj:
                if obj not in objectives:
                    objectives.append(obj)
                if obj not in obj_by_subj.setdefault(subj, []):
                    obj_by_subj[subj].append(obj)
            for key, bucket in (("domain", domains), ("theme", themes)):
                v = t.get(key, "")
                if v and v not in bucket:
                    bucket.append(v)
            concept, _ = split_explanation(t.get("explanation", ""))
            for term in quoted_terms(concept):
                if term not in terms:
                    terms.append(term)
                subj_terms = terms_by_subj.setdefault(subj, [])
                if term not in subj_terms:
                    subj_terms.append(term)
            for fact in clauses(concept):
                bucket = facts_by_subj.setdefault(subj, [])
                if fact not in bucket:
                    bucket.append(fact)
                domain = t.get("domain", "")
                if domain:
                    dbucket = facts_by_domain.setdefault((subj, domain), [])
                    if fact not in dbucket:
                        dbucket.append(fact)
        self._facts_by_domain = facts_by_domain
        self._facts_by_subject = facts_by_subj
        self._titles_by_subject = titles
        self._objectives = objectives
        self._objectives_by_subject = obj_by_subj
        self._domains = domains
        self._themes = themes
        self._terms = terms
        self._terms_by_subject = terms_by_subj
        self._title_by_id = title_by_id

    def _titles(self, topic: Dict[str, Any]) -> List[str]:
        return self._titles_by_subject.get(topic.get("subject", ""), [])

    def _objectives_for(self, topic: Dict[str, Any]) -> List[str]:
        return self._objectives_by_subject.get(topic.get("subject", ""), self._objectives)

    def _peer_facts(self, topic: Dict[str, Any]) -> List[str]:
        """同学科干扰项：优先同领域的事实要点，不足再补同学科其他领域的。"""
        subj = topic.get("subject", "")
        domain = topic.get("domain", "")
        out = list(self._facts_by_domain.get((subj, domain), [])) if domain else []
        for f in self._facts_by_subject.get(subj, []):
            if f not in out:
                out.append(f)
        return out

    @staticmethod
    def _sample(pool: List[str], exclude: Tuple[str, ...], k: int,
                rng: random.Random) -> List[str]:
        seen = set(x for x in exclude if x)
        out: List[str] = []
        for x in pool:
            if x and x not in seen:
                seen.add(x)
                out.append(x)
            if len(out) >= k:
                break
        rng.shuffle(out)
        return out

    @staticmethod
    def _ask(strategy: str, variant: int, **kw) -> str:
        tpls = TEMPLATES[strategy]
        pre = PREFIXES[variant % len(PREFIXES)]
        tpl = tpls[(variant // len(PREFIXES)) % len(tpls)]
        return pre + tpl.format(**kw)

    # ---------- 主入口 ----------
    def generate(self, topic: Dict[str, Any], n: int) -> List[Question]:
        concept, mistakes = split_explanation(topic.get("explanation", ""))
        ctx = {
            "concept": concept,
            "clauses": clauses(concept),
            "terms": quoted_terms(concept),
            "mistakes": mistakes,
            "objective": _clip(topic.get("objective", ""), 76),
        }
        strategies = self._strategies(topic, ctx)
        out: List[Question] = []
        seen_stems: set = set()
        # 组合空间 = 策略 × 问法模板 × 前缀 × 内容锚点；循环上限留冗余抗去重
        for v in range(n * 12):
            if len(out) >= n:
                break
            strategy = strategies[v % len(strategies)]
            local_v = v // len(strategies)
            rng = _rng_for(topic.get("topic_id", ""), strategy, local_v, self.seed)
            q = getattr(self, f"_s_{strategy}")(topic, ctx, local_v, rng)
            if q is None or q.stem in seen_stems:
                continue
            seen_stems.add(q.stem)
            out.append(q)
        return out[:n]

    def _strategies(self, topic: Dict[str, Any], ctx: Dict[str, Any]) -> List[str]:
        # 只保留考查知识内容的策略；元数据题（领域/目标匹配/前置）已移除。
        s: List[str] = []
        if ctx["clauses"]:
            s += ["fact_choice", "fact_choice", "fact_choice"]
        if ctx["mistakes"]:
            s += ["mistake_spot", "mistake_spot"]
        if ctx["terms"]:
            s += ["term_cloze", "term_cloze"]
        grade = topic.get("grade", "")
        is_kg = any(k in grade for k in ("小班", "中班", "大班", "幼儿园"))
        # 幼儿园数学严格执行「去小学化」：不出竖式/算式，只用内容辨析题。
        # 小学数学的数值题提供无限不重复题干，是扩量主力；初中为抽象符号
        # 运算，数值题在 _s_math_numeric 内被拒，自动退化为内容辨析题。
        if self.math_numeric and topic.get("subject") == "math" and not is_kg:
            s += ["math_numeric"] * 10
        return s or ["fact_choice"]

    def _pack(self, topic: Dict[str, Any], strategy: str, stem: str,
              correct: str, distractors: List[str], explanation: str,
              rng: random.Random, index: int, bloom: Optional[str] = None) -> Optional[Question]:
        correct = _clip(str(correct), 80)
        if len(correct) < 1 or len(stem) < 6:
            return None
        dis = [_clip(str(d), 80) for d in distractors]
        options, answer = self.shuffle_options(stem, correct, dis, rng)
        if len(options) != 4:
            return None
        return Question(
            id=make_id(topic["topic_id"], index, self.seed, f"ca_{strategy}"),
            topic_id=topic["topic_id"],
            grade=topic.get("grade", ""),
            subject=topic.get("subject", ""),
            topic=topic.get("topic", ""),
            chapter=topic.get("chapter", ""),
            difficulty=int(topic.get("difficulty", 1) or 1),
            bloom=bloom or topic.get("bloom", "理解"),
            type="single_choice",
            stem=stem,
            options=options,
            answer=answer,
            explanation=explanation,
            source="content_aware",
            generator_version=VERSION,
        )

    # ---------- 题型策略 ----------
    def _s_fact_choice(self, topic, ctx, v, rng):
        """考「怎么做才对」：正确项是 concept 的具体做法，干扰项含真实误区。"""
        facts = ctx["clauses"]
        if not facts:
            return None
        ans = _clip(facts[v % len(facts)], 80)
        obj = ctx["objective"]
        if not obj:
            return None
        stem = self._ask("fact_choice", v, obj=obj, topic=topic.get("topic", ""))
        # 干扰项：本知识点的典型误区 + 同学科其他知识点的要点（内容同质）
        pool = [m for m in ctx["mistakes"] if m != ans]
        pool += self._peer_facts(topic)
        dis = self._sample(pool, (ans,), 3, rng)
        if len(dis) < 3:
            return None
        return self._pack(topic, "fact_choice", stem, ans, dis,
                          f"“{ans}”是「{topic.get('topic','')}」讲解中的正确做法，"
                          f"契合学习目标“{obj}”；其余是常见误区或其他内容的做法。",
                          rng, v, bloom="应用")

    def _s_mistake_spot(self, topic, ctx, v, rng):
        """考「哪种做法是错的」：正确项是典型误区，干扰项是正确做法。"""
        mistakes = ctx["mistakes"]
        if not mistakes:
            return None
        ans = _clip(mistakes[v % len(mistakes)], 80)
        obj = ctx["objective"]
        stem = self._ask("mistake_spot", v, obj=obj, topic=topic.get("topic", ""))
        pool = list(ctx["clauses"])
        pool += self._peer_facts(topic)
        dis = self._sample(pool, (ans,), 3, rng)
        if len(dis) < 3:
            return None
        return self._pack(topic, "mistake_spot", stem, ans, dis,
                          f"“{ans}”是「{topic.get('topic','')}」讲解中明确列出的常见误区；"
                          f"其余为符合要求或有价值的做法。",
                          rng, v, bloom="分析")

    def _s_term_cloze(self, topic, ctx, v, rng):
        """考核心要领：把 concept 中含「术语」的原句挖空，术语为答案。"""
        local = ctx["terms"]
        if not local:
            return None
        ans = local[v % len(local)]
        # 找到含该术语的原句作为锚点（保证题干本身携带内容语境）
        anchor = ""
        for fact in ctx["clauses"]:
            if f"「{ans}」" in fact:
                anchor = fact.replace(f"「{ans}」", "＿＿")
                break
        if not anchor:
            anchor = _clip(topic.get("topic", ""), 46) + "的关键要领是＿＿"
        stem = self._ask("term_cloze", v, anchor=_clip(anchor, 60))
        # 干扰项取同学科其他知识点的术语，避免跨科串味
        peer = self._terms_by_subject.get(topic.get("subject", ""), [])
        dis = self._sample(peer, (ans,), 3, rng)
        if len(dis) < 3:
            return None
        return self._pack(topic, "term_cloze", stem, ans, dis,
                          f"「{ans}」是「{topic.get('topic','')}」概念讲解中的关键术语；"
                          f"其余术语属于同学科其他知识点。",
                          rng, v, bloom="理解")

    def _s_math_numeric(self, topic, ctx, v, rng):
        """数值题：只在章节/主题确实是运算类时出题，避免给位置、统计、
        图形认识等知识点硬套算式；并按年级限制运算种类（一年级不出乘法）。
        """
        name = topic.get("topic", "")
        chapter = topic.get("chapter", "")
        theme = topic.get("theme", "")
        grade = topic.get("grade", "")

        # 数值算式题只适用于小学。初中数学（整式/方程/函数/几何证明等）
        # 都是抽象符号运算，套「小明买苹果」的纯数值题会驴唇不对马嘴。
        is_primary = any(k in grade for k in
                         ("小班", "中班", "大班", "一年级", "二年级", "三年级",
                          "四年级", "五年级", "六年级"))
        if not is_primary:
            return None

        # 只用「知识点名称 + 章节」判定算式类型（theme 太宽泛，如
        # 「图形的认识与测量」会让所有图形题都去算面积）。且必须精确匹配
        # 运算类章节，概念/位置/统计/推理类知识点直接放弃数值题。
        field = f"{name} {chapter}"

        # 运算类型按年级划分：一二年级只有加减，三年级起才引入乘除
        low = any(k in grade for k in ("小班", "中班", "大班", "一年级", "二年级"))
        mid = any(k in grade for k in ("三年级", "四年级"))

        # 几何计算：必须是明确的面积/周长/体积计算知识点
        geom = any(k in field for k in ("面积", "周长", "体积")) or \
            (any(k in field for k in ("圆", "三角形")) and
             any(k in field for k in ("面积", "周长", "计算", "半径", "直径")))
        # 百分数/比例计算
        pct = any(k in field for k in ("百分数", "百分比", "折扣", "利率", "成数"))
        # 方程：必须名称或章节明确是方程
        eq = any(k in field for k in ("方程", "未知数")) and not low
        # 分数运算：名称/章节明确是分数运算（不含"分数的初步认识"这类概念课）
        frac = any(k in field for k in ("分数", "约分", "通分")) and \
            any(k in field for k in ("加", "减", "乘", "除", "计算", "运算")) and not low
        # 加减乘除四则：章节名含明确的运算词
        arith = any(k in field for k in ("加法", "减法", "乘法", "除法", "加减", "四则",
                                        "混合运算", "口算", "运算定律", "的加减法"))

        if pct:
            maker, kind = self._legacy._gen_percentage, "percentage"
        elif eq:
            maker, kind = self._legacy._gen_equation, "equation"
        elif frac:
            maker, kind = self._legacy._gen_fraction, "fraction"
        elif geom:
            maker, kind = self._legacy._gen_geometry, "geometry"
        elif arith:
            maker, kind = self._legacy._gen_arithmetic, "arithmetic"
        else:
            # 概念/图形认识/位置/统计/推理类知识点不适合数值题，放弃
            return None

        # 限制 _gen_arithmetic 的随机 mode：低年级禁乘，中高年级禁过早引入乘
        if kind == "arithmetic" and low:
            mode = rng.choice(["add", "sub", "word"])
            return self._arithmetic_with_mode(topic, v, rng, mode)
        if kind == "arithmetic" and mid:
            mode = rng.choice(["add", "sub", "mul", "word"])
            return self._arithmetic_with_mode(topic, v, rng, mode)

        q = maker(topic, v, rng)
        if q is None:
            return None
        q.id = make_id(topic["topic_id"], v, self.seed, f"ca_{kind}")
        q.source = "content_aware"
        q.generator_version = VERSION
        q.bloom = topic.get("bloom", "应用")
        q.difficulty = int(topic.get("difficulty", 1) or 1)
        return q

    def _arithmetic_with_mode(self, topic, v, rng, mode) -> Optional[Question]:
        """复用旧引擎的加/减/应用题构造，但强制指定运算种类。"""
        from generators.template import TemplateGenerator as T
        lo, hi = T._num_range(topic["grade"])
        span = 0 if hi <= 20 else max((hi - lo) // 2, 10)
        a, b = rng.randint(lo, hi + span), rng.randint(lo, hi + span)
        if mode == "word":
            scene, unit = rng.choice([
                ("小明有{a}个苹果，又买了{b}个，现在一共有", "个苹果"),
                ("书架上有{a}本书，又放上{b}本，共有", "本书"),
                ("商店运来{a}箱牛奶，下午又运来{b}箱，全天共运来", "箱"),
            ])
            c = a + b
            stem = scene.format(a=a, b=b) + "多少" + unit + "？"
            expl = f"{a} + {b} = {c}"
            dis = [str(c + rng.randint(1, 9)), str(a), str(b)]
        elif mode == "add":
            c = a + b
            stem = f"{a} + {b} = ?"
            expl = f"{a} + {b} = {c}"
            dis = [str(c + rng.randint(1, 9)), str(max(0, c - rng.randint(1, 9))), str(abs(a - b))]
        else:  # sub
            a, b = max(a, b), min(a, b)
            c = a - b
            stem = f"{a} - {b} = ?"
            expl = f"{a} - {b} = {c}"
            dis = [str(c + rng.randint(1, 9)), str(max(0, c - rng.randint(1, 9))), str(a + b)]
        q = self._legacy._make(topic, v, "arithmetic", stem, str(c), dis, expl, rng)
        if q is None:
            return None
        q.id = make_id(topic["topic_id"], v, self.seed, "ca_arithmetic")
        q.source = "content_aware"
        q.generator_version = VERSION
        q.bloom = topic.get("bloom", "应用")
        q.difficulty = int(topic.get("difficulty", 1) or 1)
        return q
