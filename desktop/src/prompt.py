#!/usr/bin/env python3
"""为单个知识点生成「大模型出题提示词」。

设计目标：把知识点的完整档案（考核要求、核心概念、易错点、年级认知水平等）
组织成一份可直接复制到 DeepSeek / 元宝 / ChatGPT 的指令，让模型按 JSON
Schema 输出高质量选择题。

关键约束（对应用户反馈）：
  - 禁止把知识点名称当题干主语、禁止「属于哪个领域 / 学习目标是什么 /
    前置知识点是谁」这类档案题——考的是知识内容本身。
  - 干扰项优先用本知识点的易错点，且贴合年级认知水平。
  - 输出严格 JSON 数组，便于后续导入题库。
仅依赖 Python 标准库。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

STAGE_CN = {"kg": "幼儿园", "primary": "小学", "junior": "初中"}
SUBJECT_CN = {"chinese": "语文", "math": "数学", "english": "英语", "science": "科学"}
GRADE_CN = {
    "k1": "小班", "k2": "中班", "k3": "大班",
    "g1": "一年级", "g2": "二年级", "g3": "三年级",
    "g4": "四年级", "g5": "五年级", "g6": "六年级",
    "g7": "七年级", "g8": "八年级", "g9": "九年级",
}
TERM_CN = {"s1": "上册", "s2": "下册"}
# 难度展示名与界面保持一致（JSON 字段仍用数字 1-4）
DIFFICULTY_CN = {1: "容易", 2: "中等", 3: "较难", 4: "难"}

# 按学科给的 few-shot 示例：只展示「情境化题干 + 内容型选项 + 解析」的范式，
# 不照抄任何真实知识点，避免模型把示例内容当成答案来源。
EXAMPLES: Dict[str, str] = {
    "chinese": """示例（仅参考题型风格，不要照抄内容）：
知识点：小学语文「用音序查字法查字典」；学习目标：能按音序在字典中查到指定汉字；核心要点：先查大写字母再查音节；易错点：把声母当音序、声调标错。
输出示例：
[
  {
    "stem": "要查「帆」字，按音序查字法应先查下面的哪一项？",
    "options": ["大写字母 F", "声母 f", "部首「巾」", "笔画数 6"],
    "answer": "A",
    "explanation": "音序查字法先查音节首字母的大写形式，帆读 fān，音序是 F。声母 f 不是音序，部首属于部首查字法。",
    "difficulty": 2,
    "bloom": "应用",
    "type": "single_choice",
    "topic_name": "示例知识点",
    "topic_id": "example-001"
  }
]""",
    "math": """示例（仅参考题型风格，不要照抄内容）：
知识点：小学数学「长方形周长的计算」；学习目标：能计算长方形的周长；核心要点：周长 = (长 + 宽) × 2；易错点：只算长加宽、忘乘 2。
输出示例：
[
  {
    "stem": "一个长方形花坛长 8 米、宽 5 米，绕它走一圈是多少米？",
    "options": ["26 米", "13 米", "40 米", "21 米"],
    "answer": "A",
    "explanation": "绕一圈即周长，(8+5)×2=26 米。13 米只是长加宽，忘了乘 2；40 米是误把长乘宽当作周长。",
    "difficulty": 2,
    "bloom": "应用",
    "type": "single_choice",
    "topic_name": "示例知识点",
    "topic_id": "example-001"
  }
]""",
    "english": """示例（仅参考题型风格，不要照抄内容）：
知识点：小学英语「用 Hello / Hi 问候」；学习目标：能在见面情境中用 Hello 打招呼；核心要点：见面用 Hello/Hi，回答也用 Hello/Hi；易错点：把 Hello 当告别语、与 Thank you 混用。
输出示例：
[
  {
    "stem": "早上见到同学，下面哪一句是最合适的问候？",
    "options": ["Hello!", "Goodbye!", "Thank you!", "Sorry!"],
    "answer": "A",
    "explanation": "Hello 用于见面问候；Goodbye 用于告别，Thank you 表感谢，Sorry 表道歉，都不符合见面场景。",
    "difficulty": 1,
    "bloom": "应用",
    "type": "single_choice",
    "topic_name": "示例知识点",
    "topic_id": "example-001"
  }
]""",
}
DEFAULT_EXAMPLE = EXAMPLES["chinese"]


def _grade_label(grade: str, term: str, stage: str) -> str:
    g = GRADE_CN.get(grade, grade)
    if stage == "kg":
        return g
    return g + TERM_CN.get(term, "")


def _buddy_age_note(stage: str, grade: str) -> str:
    """给模型一条年级认知水平提示，避免超纲。"""
    if stage == "kg":
        return ("这是幼儿园内容，题目必须是游戏化、生活化的情境题，"
                "不出算式、不考书写与抽象符号，语言简短具体，每题题干不超过 40 字。")
    if grade in ("g1", "g2"):
        return "这是小学低年级内容，题干要简短、情境具体，避免复杂长句和多步推理。"
    if grade in ("g7", "g8", "g9"):
        return "这是初中内容，题目应体现概念理解与简单应用，可含必要的符号表达。"
    return "题目难度与语言须符合该年级学生的认知水平，不出超纲内容。"


def _grade_redline(stage: str, grade: str) -> str:
    """硬约束文本：明确禁止超出该知识点所属年级的课标水平。"""
    if stage == "kg":
        return ("本题组属于幼儿园水平，**严禁超出当前年级水平**：不得出现任何加减乘除算式、"
                "数字计算、拼音书写、汉字抄写、抽象符号，也不得引入小学阶段才要求的知识与技能；"
                "只能用游戏化、生活化的口头情境。")
    if grade in ("g1", "g2"):
        return ("本题组属于小学低年级水平，**严禁超出当前年级水平**：不得出现多步混合运算、"
                "分数与小数运算、复杂长句与多步推理，不得引入三年级及以上才学习的概念、"
                "字词、句式或运算规则。")
    if grade in ("g3", "g4"):
        return ("本题组属于小学中年级水平，**严禁超出当前年级水平**：不得引入分数乘除、"
                "方程、几何证明、高年级文言文实词虚词等超出本年级课标的内容，"
                "运算不得超出现学运算种类。")
    if grade in ("g5", "g6"):
        return ("本题组属于小学高年级水平，**严禁超出当前年级水平**：不得引入初中的方程、"
                "函数、几何证明、理化概念，不得使用初中英语语法术语或超出小学范围的文言文知识。")
    if grade in ("g7", "g8", "g9"):
        return ("本题组属于初中水平，**严禁超出当前年级水平**：不得引入高中阶段才学习的"
                "函数导数、解析几何、立体几何证明、复杂语法与生活经验，也不得提前使用后续年级内容。")
    return "本题组**严禁超出当前选择年级的水平**，不得使用该年级课标范围之外的概念与技能。"


def allocate_difficulty(n: int, mix: Optional[Dict[int, int]]) -> Dict[int, int]:
    """按四级占比把 n 道题分配到难度 1-4（最大余数法，保证总数恰为 n）。

    mix 为 {难度: 百分比}；无有效占比时全部落到难度 2（兼容旧行为）。
    """
    ratios = {d: max(0, int((mix or {}).get(d, 0) or 0)) for d in (1, 2, 3, 4)}
    if sum(ratios.values()) <= 0 or n <= 0:
        return {1: 0, 2: n, 3: 0, 4: 0}
    raw = {d: n * ratios[d] / sum(ratios.values()) for d in ratios}
    alloc = {d: int(raw[d]) for d in ratios}
    order = sorted(ratios, key=lambda d: (-(raw[d] - alloc[d]), -ratios[d]))
    for d in order[:n - sum(alloc.values())]:
        alloc[d] += 1
    return alloc


def difficulty_spec(n: int, mix: Optional[Dict[int, int]],
                    default_level: int = 2) -> str:
    """把难度分配转成一句可直接写进提示词的说明。"""
    alloc = allocate_difficulty(n, mix)
    if mix and sum(max(0, int(v or 0)) for v in mix.values()) > 0:
        return "；".join(f"{DIFFICULTY_CN[d]}（对应 difficulty={d}）{alloc[d]} 题"
                        for d in (1, 2, 3, 4))
    level = int(default_level or 2)
    return f"{DIFFICULTY_CN.get(level, '中等')}（对应 difficulty={level}）"


def build_prompt(kp: Dict[str, Any], vol: Dict[str, Any], n: int = 5,
                 title_by_id: Optional[Dict[str, str]] = None,
                 difficulty_mix: Optional[Dict[int, int]] = None) -> str:
    """为单个知识点生成完整的大模型出题提示词。

    kp: 知识点 dict（volumes/*.json 里的原始字段）
    vol: 册元数据 dict
    n: 期望生成的题目数
    title_by_id: 可选，前置知识点 ID → 名称 的映射
    """
    stage = vol.get("stage", "")
    grade = vol.get("grade", "")
    subject = vol.get("subject", "")
    term = vol.get("term", "")
    grade_label = _grade_label(grade, term, stage)

    # 前置知识点：能查到名字就用名字，查不到保留 ID
    pre_ids = kp.get("prerequisites") or []
    title_by_id = title_by_id or {}
    pre_text = "、".join(title_by_id.get(pid, pid) for pid in pre_ids) or "无"

    mistakes = kp.get("common_mistakes") or []
    mistakes_text = "；".join(str(m) for m in mistakes) or "（该知识点未标注易错点，请依据核心概念设计合理干扰项）"
    types_text = "、".join(kp.get("suggested_types") or []) or "未指定"
    band = DIFFICULTY_CN.get(int(kp.get("difficulty", 2) or 2), "理解应用")

    profile = f"""| 字段 | 内容 |
|---|---|
| 学段 / 年级 | {STAGE_CN.get(stage, stage)} · {grade_label} |
| 科目 | {SUBJECT_CN.get(subject, subject)} |
| 课标领域 | {kp.get('domain', '')} |
| 主题 | {kp.get('theme', '')} |
| 单元 | {kp.get('unit', '')} |
| 知识点名称 | {kp.get('name', '')} |
| **学习目标（考核要求）** | {kp.get('objective', '')} |
| 难度 | {kp.get('difficulty', 2)}（{band}） |
| 认知层级 | {kp.get('bloom', '理解')} |
| 核心概念 / 讲解 | {kp.get('concept', '')} |
| **易错点** | {mistakes_text} |
| 前置知识 | {pre_text} |
| 建议题型 | {types_text} |
| 权威来源 | {kp.get('source', '')} |"""

    example = EXAMPLES.get(subject, DEFAULT_EXAMPLE)
    spec = difficulty_spec(n, difficulty_mix, int(kp.get("difficulty", 2) or 2))

    return f"""你是一位资深的中小学命题专家，熟悉中国教育部课程标准和《3-6岁儿童学习与发展指南》，擅长根据知识点命制高质量的选择题。

## 任务
根据下面的知识点，命制 **{n} 道单项选择题**。
难度分布（必须严格执行）：{spec}。

## 知识点档案
{profile}

## 命题规则（必须逐条遵守）
1. **考内容，不考档案**：题目必须考查学生对「学习目标」所描述的知识、技能或能力的掌握。严禁把知识点名称当作题干主语来问，禁止出现下列题型：
   - ✗「知识点『X』属于哪个领域？」
   - ✗「以下哪项是『X』的学习目标？」
   - ✗「学习『X』之前应先掌握哪个知识点？」
   - ✗「『X』对应哪个主题 / 课标要求？」
   要创设真实的学习或生活情境，让学生运用知识作答，而不是复述概念定义。
2. **正确项唯一且明确**：正确答案必须能从「核心概念 / 学习目标」中明确推出，不得模棱两可；同一题只有一个正确答案。
3. **干扰项有迷惑性且同质**：至少 1 个干扰项直接取自或改编自本知识点的「易错点」；其余干扰项应是同学科、同主题下学生常见的错误认识，长度和表述风格与正确项相当。禁止出现明显荒谬、跨科串味或与题干无关的干扰项。
4. **年级红线（硬性约束）**：本知识点属于{grade_label}（{STAGE_CN.get(stage, stage)}）水平。**题目禁止超出当前选择年级的水平**。{_grade_redline(stage, grade)}
   同时注意：{_buddy_age_note(stage, grade)}
5. **难度分布**：严格按上面「难度分布」指定的各难度题数出题；每题的 difficulty 字段必须填写该题实际难度对应的数字（1=容易、2=中等、3=较难、4=难），不得随意填写或与题目要求不符。
6. **避免机械重复**：{n} 道题的题干要有不同的情境和问法，不要只是替换个别词语的同一道题。
7. **解析到位**：每题解析要说明正确项为什么对、干扰项为什么错，能帮助学生理解。
8. **选项规范**：每题 4 个选项（A/B/C/D），正确答案的位置尽量分散，不要集中在某一项。

## 输出格式（严格遵守）
只输出一个 JSON 数组，不要任何 Markdown 代码块标记、不要解释文字、不要前后缀。每个元素格式如下（difficulty 按该题实际难度填 1/2/3/4，不要照抄示例里的值）：
[
  {{
    "topic_id": "{kp.get('id', '')}",
    "topic_name": "{kp.get('name', '')}",
    "stem": "题干文字",
    "options": ["A 选项内容", "B 选项内容", "C 选项内容", "D 选项内容"],
    "answer": "A",
    "explanation": "解析文字",
    "difficulty": {int(kp.get('difficulty', 2) or 2)},
    "bloom": "{kp.get('bloom', '理解')}",
    "type": "single_choice"
  }}
]
每道题都必须原样带上 topic_id 和 topic_name 两个字段，标明该题来源于上面「知识点档案」中的这个知识点，便于入库后溯源。

{example}

## 现在开始
请针对上面的知识点，输出 {n} 道符合全部规则的题目（严格 JSON 数组）。"""


def build_batch_prompt(points: List[Dict[str, Any]], n_per: int = 3,
                       title_by_id: Optional[Dict[str, str]] = None,
                       difficulty_mix: Optional[Dict[int, int]] = None) -> str:
    """为多个知识点生成一条批量提示词（每点 n_per 题）。"""
    title_by_id = title_by_id or {}
    chunks = []
    for i, item in enumerate(points, 1):
        kp, vol = item["kp"], item["vol"]
        stage, grade = vol.get("stage", ""), vol.get("grade", "")
        grade_label = _grade_label(grade, vol.get("term", ""), stage)
        chunks.append(
            f"### 第 {i} 题组 · {kp.get('name', '')}（{STAGE_CN.get(stage, '')}"
            f"{grade_label}{SUBJECT_CN.get(vol.get('subject',''), '')}）\n"
            f"- 知识点 ID：{kp.get('id', '')}\n"
            f"- 学习目标：{kp.get('objective', '')}\n"
            f"- 核心概念：{kp.get('concept', '')}\n"
            f"- 易错点：{'；'.join(str(m) for m in (kp.get('common_mistakes') or [])) or '无'}\n"
            f"- 难度：{kp.get('difficulty', 2)} · {kp.get('bloom', '理解')}\n"
            f"- 年级红线：{_grade_redline(stage, grade)}"
        )
    spec = difficulty_spec(n_per, difficulty_mix)
    return f"""你是一位资深的中小学命题专家。请为下面 {len(points)} 个知识点各命制 {n_per} 道单项选择题（共 {len(points) * n_per} 道）。
每组难度分布（必须严格执行）：{spec}。

## 命题规则（必须逐条遵守）
1. **考内容，不考档案**：围绕每个知识点的「学习目标」创设情境题，严禁把知识点名称当题干来问，禁止「属于哪个领域 / 学习目标是什么 / 前置知识点是谁」这类档案题。
2. 正确项唯一明确，能从核心概念推出；至少 1 个干扰项取自该知识点的易错点，其余为同学科同主题的典型错误认识；禁止明显荒谬或跨科串味的选项。
3. **年级红线（硬性约束）**：题目禁止超出当前选择年级的水平。每组题目必须严格遵守该组知识点标注的「年级红线」，不得使用该年级课标范围之外的概念、运算、词汇与推理难度。
4. 每组难度分布按上面的指定执行；每题 difficulty 字段填写实际难度数字（1/2/3/4）。题干情境化、不机械重复。
5. 每组题目之间用 topic_name 字段标明所属知识点，不要混淆。

## 知识点清单
{chr(10).join(chunks)}

## 输出格式（严格遵守）
只输出一个 JSON 数组，不要 Markdown 代码块标记、不要解释文字。每道题都必须带 topic_id 和 topic_name 两个字段，原样填写该题所属知识点清单中的 id 与名称，标明题目来源以便入库溯源：
[
  {{
    "topic_id": "知识点ID",
    "topic_name": "知识点名称",
    "stem": "题干文字",
    "options": ["A 选项", "B 选项", "C 选项", "D 选项"],
    "answer": "A",
    "explanation": "解析文字",
    "difficulty": 2,
    "bloom": "理解",
    "type": "single_choice"
  }}
]

## 示例（参考风格）
{DEFAULT_EXAMPLE}

## 现在开始
请输出 {len(points) * n_per} 道符合全部规则的题目（严格 JSON 数组）。"""
