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
DIFFICULTY_CN = {1: "基础识记", 2: "理解应用", 3: "分析综合", 4: "挑战探究"}

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
    "type": "single_choice"
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
    "type": "single_choice"
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
    "type": "single_choice"
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


def build_prompt(kp: Dict[str, Any], vol: Dict[str, Any], n: int = 5,
                 title_by_id: Optional[Dict[str, str]] = None) -> str:
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

    return f"""你是一位资深的中小学命题专家，熟悉中国教育部课程标准和《3-6岁儿童学习与发展指南》，擅长根据知识点命制高质量的选择题。

## 任务
根据下面的知识点，命制 **{n} 道单项选择题**。

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
4. **年级适配**：{_buddy_age_note(stage, grade)}
5. **避免机械重复**：{n} 道题的题干要有不同的情境和问法，不要只是替换个别词语的同一道题。
6. **解析到位**：每题解析要说明正确项为什么对、干扰项为什么错，能帮助学生理解。
7. **选项规范**：每题 4 个选项（A/B/C/D），正确答案的位置尽量分散，不要集中在某一项。

## 输出格式（严格遵守）
只输出一个 JSON 数组，不要任何 Markdown 代码块标记、不要解释文字、不要前后缀。每个元素格式如下：
[
  {{
    "stem": "题干文字",
    "options": ["A 选项内容", "B 选项内容", "C 选项内容", "D 选项内容"],
    "answer": "A",
    "explanation": "解析文字",
    "difficulty": {int(kp.get('difficulty', 2) or 2)},
    "bloom": "{kp.get('bloom', '理解')}",
    "type": "single_choice"
  }}
]

{example}

## 现在开始
请针对上面的知识点，输出 {n} 道符合全部规则的题目（严格 JSON 数组）。"""


def build_batch_prompt(points: List[Dict[str, Any]], n_per: int = 3,
                       title_by_id: Optional[Dict[str, str]] = None) -> str:
    """为多个知识点生成一条批量提示词（每点 n_per 题）。"""
    title_by_id = title_by_id or {}
    chunks = []
    for i, item in enumerate(points, 1):
        kp, vol = item["kp"], item["vol"]
        chunks.append(
            f"### 第 {i} 题组 · {kp.get('name', '')}（{STAGE_CN.get(vol.get('stage',''), '')}"
            f"{_grade_label(vol.get('grade',''), vol.get('term',''), vol.get('stage',''))}"
            f"{SUBJECT_CN.get(vol.get('subject',''), '')}）\n"
            f"- 学习目标：{kp.get('objective', '')}\n"
            f"- 核心概念：{kp.get('concept', '')}\n"
            f"- 易错点：{'；'.join(str(m) for m in (kp.get('common_mistakes') or [])) or '无'}\n"
            f"- 难度：{kp.get('difficulty', 2)} · {kp.get('bloom', '理解')}"
        )
    return f"""你是一位资深的中小学命题专家。请为下面 {len(points)} 个知识点各命制 {n_per} 道单项选择题（共 {len(points) * n_per} 道）。

## 命题规则（必须逐条遵守）
1. **考内容，不考档案**：围绕每个知识点的「学习目标」创设情境题，严禁把知识点名称当题干来问，禁止「属于哪个领域 / 学习目标是什么 / 前置知识点是谁」这类档案题。
2. 正确项唯一明确，能从核心概念推出；至少 1 个干扰项取自该知识点的易错点，其余为同学科同主题的典型错误认识；禁止明显荒谬或跨科串味的选项。
3. 题干情境化、不机械重复；题目难度与语言符合对应年级认知水平。
4. 每组题目之间用知识点名称区分，不要混淆知识点。

## 知识点清单
{chr(10).join(chunks)}

## 输出格式（严格遵守）
只输出一个 JSON 数组，不要 Markdown 代码块标记、不要解释文字。每个元素额外带 "topic_name" 字段标明所属知识点：
[
  {{
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
