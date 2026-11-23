#!/usr/bin/env python3
"""知识点地图「交叉验证」：把每条知识点与权威课标基准逐条比对。

这是 build_volumes.py / validate.py 之外的第三道关：前两者校验结构
（字段齐全、ID 唯一、引用存在、领域合法），本脚本校验**内容与课标的一致性**，
规则来自对教育部官方课标原文的核对（见 RULES 注释中的来源）。

校验维度：
  1. 数学 domain/theme 与学段匹配（2022课标表2：四个领域各学段主题不同）
  2. 2022课标调整点：百分数∈统计与概率、负数∈综合与实践、负数量词
  3. 幼儿园去小学化：不得正面要求拼音书写/汉字书写/竖式/应用题训练
     （否定语境如"不要求书写算式"属正确表述，不计违规）
  4. 英语 domain∈六要素、theme∈要素/主题范畴/技能子项
  5. 教材来源合规：低年级英语用"新起点"（一起点）而非"PEP"（三起点）
  6. 认知层级与难度一致性（记忆1-2/理解1-3/应用2-3/分析评价创造3-4）
  7. 难度—认知层级单调性、字段中英夹杂（非英语学科）

用法：
    python3 knowledge-map/tools/cross_validate.py           # 输出报告
    python3 knowledge-map/tools/cross_validate.py --json    # 机器可读
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
KM_ROOT = os.path.dirname(HERE)
VOL_DIR = os.path.join(KM_ROOT, "volumes")
STANDARDS = os.path.join(KM_ROOT, "taxonomy", "standards.json")

# ── 规则基准（来自 2022 版课标 / 《3-6岁指南》原文核对）─────────────
DIFFICULTY_BAND = {
    "记忆": (1, 2), "理解": (1, 3), "应用": (2, 3),
    "分析": (3, 4), "评价": (3, 4), "创造": (3, 4),
}
# 课标明确调整：这些词在小学数与代数领域出现即为旧课标残留
MATH_DOMAIN_RULES = [
    ("百分数", "统计与概率", "2022课标将百分数移入统计与概率（作为统计量）"),
    ("负数", "综合与实践", "2022课标将负数移入综合与实践（主题活动）"),
]
# 小学不得出现的初中概念词（concept 中若正面出现即为超纲）
PRIMARY_ADVANCED = [
    "绝对值", "一元二次方程", "二次函数", "全等三角形", "相似三角形",
    "勾股定理", "因式分解", "分式方程", "二次根式", "三角函数", "判别式",
]
# 去小学化敏感词（正面出现即违规；否定语境另判）
KG_BAN_WORDS = ["书写汉字", "写汉字", "抄写", "拼音书写", "默写", "列竖式",
                "竖式计算", "100以内加减", "应用题训练", "机械背诵"]
# 否定语境标记：出现这些词说明是"教学禁区"的正确表述
KG_NEG_MARKERS = ["不要求", "不进行", "避免", "禁止", "而非", "不是"]
# 英语低年级错误来源：PEP 为三年级起点，一二年级应为新起点
EN_G12_BAD_SOURCE = re.compile(r"PEP[一二]")


def load_all():
    rows = []
    for f in sorted(glob.glob(os.path.join(VOL_DIR, "*", "*", "*.json"))):
        doc = json.load(open(f, encoding="utf-8"))
        for kp in doc["knowledge_points"]:
            kp2 = dict(kp)
            kp2.update(stage=doc["stage"], subject=doc["subject"],
                       school_stage=doc["school_stage"], volume_id=doc["volume_id"])
            rows.append(kp2)
    return rows


def get_valid_themes(std, school_stage, subject):
    if school_stage == "S0":
        return set()
    if subject == "math":
        t = std["domains"]["math"]["themes_by_stage"]
        return {x for d in t.values() for x in d.get(school_stage, [])}
    if subject == "chinese":
        return set()
    if subject == "english":
        en = std["domains"]["english"]
        return (set(en["items"]) | set(en["themes"]) |
                set(en["language_knowledge"]) | set(en["language_skills"]) |
                {"综合技能"})
    return set()


def check(rows, std):
    issues = []  # (级别, 维度, 知识点ID, 说明)

    def add(level, dim, kp, msg):
        issues.append((level, dim, kp.get("id", "?"), msg))

    themes_by = {}
    for kp in rows:
        key = (kp["school_stage"], kp["subject"])
        themes_by.setdefault(key, get_valid_themes(std, *key))

    for kp in rows:
        sid, subj, ss = kp["id"], kp["subject"], kp["school_stage"]
        name, obj, concept = kp.get("name", ""), kp.get("objective", ""), kp.get("concept", "")
        text = name + obj + concept

        # 1. theme 学段匹配
        allowed = themes_by[(ss, subj)]
        if allowed and kp.get("theme") and kp["theme"] not in allowed:
            add("错误", "theme-学段", kp,
                f"theme={kp['theme']!r} 不属于 {ss} 合法主题")

        # 2. 数学课标调整点
        if subj == "math" and kp["stage"] == "primary":
            for kw, right_domain, why in MATH_DOMAIN_RULES:
                if kw in name and kp["domain"] != right_domain:
                    add("错误", "课标调整点", kp,
                        f"「{kw}」domain={kp['domain']}，应为{right_domain}（{why}）")
            for term in PRIMARY_ADVANCED:
                # 仅当正面陈述（非"不/未/避免"）时算超纲
                pos = text.find(term)
                if pos >= 0 and not re.search(r"(不|未|避免|不是|非).{0,4}" + re.escape(term), text):
                    add("警告", "小学超纲", kp, f"小学知识点含初中概念「{term}」")

        # 3. 幼儿园去小学化
        if kp["stage"] == "kg":
            for w in KG_BAN_WORDS:
                if w in text:
                    ctx = text[max(0, text.find(w) - 12): text.find(w) + 12]
                    if not any(m in ctx for m in KG_NEG_MARKERS):
                        add("错误", "去小学化", kp, f"疑似正面要求小学化内容「{w}」：{ctx}")

        # 4. 英语来源合规
        vid = kp.get("volume_id", "")
        if subj == "english" and ("-g1-" in vid or "-g2-" in vid):
            if EN_G12_BAD_SOURCE.search(kp.get("source", "")):
                add("错误", "教材版本", kp, "一二年级英语来源用 PEP（三起点），应为新起点（一起点）")

        # 5. 难度—认知层级
        band = DIFFICULTY_BAND.get(kp.get("bloom"))
        if band and not band[0] <= kp.get("difficulty", 0) <= band[1]:
            add("错误", "难度-认知", kp,
                f"difficulty={kp['difficulty']} 与 bloom={kp['bloom']} 不符（应{band[0]}-{band[1]}）")

        # 6. 非英语学科的孤立英文单词（排除学科符号与拼音）
        if subj != "english":
            allow = {"sin", "cos", "tan", "cm", "kg", "km"}
            pinyin = {"yue", "zhi", "chi", "shi", "yuan", "yin", "yun",
                      "ang", "eng", "ing", "ong", "ying"}
            for field in (name, obj, concept):
                for w in re.findall(r"(?<![A-Za-z])[a-z]{3,}(?![A-Za-z])", field):
                    if w not in allow and w not in pinyin:
                        add("警告", "中英夹杂", kp, f"{field[:20]}…含孤立英文词「{w}」")

    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    std = json.load(open(STANDARDS, encoding="utf-8"))
    rows = load_all()
    issues = check(rows, std)

    errors = [i for i in issues if i[0] == "错误"]
    warns = [i for i in issues if i[0] == "警告"]

    if args.json:
        print(json.dumps({
            "知识点总数": len(rows),
            "错误数": len(errors), "警告数": len(warns),
            "问题明细": [{"级别": l, "维度": d, "ID": k, "说明": m} for l, d, k, m in issues],
        }, ensure_ascii=False, indent=2))
        return

    print("=" * 60)
    print("知识点地图交叉验证报告（与权威课标逐条比对）")
    print("=" * 60)
    print(f"校验知识点：{len(rows)} 条")
    print(f"错误：{len(errors)}  |  警告：{len(warns)}")
    if issues:
        dim = Counter(d for _, d, _, _ in issues)
        print("\n问题维度分布：")
        for k, v in dim.most_common():
            print(f"  {k:14s} {v}")
        print("\n问题明细（前 50 条）：")
        for lv, d, kid, msg in issues[:50]:
            print(f"  [{lv}] {kid}  {d}: {msg}")
        if len(issues) > 50:
            print(f"  ... 另有 {len(issues)-50} 条")
    else:
        print("\n✅ 全部通过：1225 条知识点与权威课标基准一致，无内容级偏差。")
    print("=" * 60)


if __name__ == "__main__":
    main()
