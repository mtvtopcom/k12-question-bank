#!/usr/bin/env python3
"""合并所有已完成册 → 统一知识点清单，作为题目生成管道的唯一数据源。

输出 knowledge-map/output/knowledge_points.jsonl，字段包含
topic_id/grade/subject/topic/explanation 等，供 pipeline 直接消费。

同时输出一份校验报告：知识点总数、分科分布、学段覆盖。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QB_ROOT = os.path.dirname(ROOT)  # question-bank/

GRADE_ORDER = {
    "k1": 0, "k2": 1, "k3": 2,
    "g1": 3, "g2": 4, "g3": 5, "g4": 6, "g5": 7, "g6": 8,
    "g7": 9, "g8": 10, "g9": 11,
}
GRADE_CN = {
    "k1": "小班", "k2": "中班", "k3": "大班",
    "g1": "一年级", "g2": "二年级", "g3": "三年级", "g4": "四年级",
    "g5": "五年级", "g6": "六年级", "g7": "七年级", "g8": "八年级", "g9": "九年级",
}
TERM_CN = {"full": "", "s1": "上", "s2": "下"}


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def kp_to_topic(vol: dict, kp: dict) -> dict:
    """知识点 → 题目生成管道的知识点格式。"""
    grade = GRADE_CN.get(vol["grade"], vol["grade"]) + TERM_CN.get(vol["term"], "")
    explanation = kp.get("concept", "")
    mistakes = kp.get("common_mistakes", [])
    if mistakes:
        explanation += "\n易错点：" + "；".join(mistakes[:3])
    return {
        "topic_id": kp["id"],
        "grade": grade,
        "subject_cn": {"chinese": "语文", "math": "数学", "english": "英语"}.get(vol["subject"], vol["subject"]),
        "subject": vol["subject"],
        "topic": kp["name"],
        "chapter": kp.get("unit", ""),
        "domain": kp.get("domain", ""),
        "theme": kp.get("theme", ""),
        "explanation": explanation,
        "objective": kp.get("objective", ""),
        "difficulty": kp.get("difficulty", 1),
        "bloom": kp.get("bloom", "理解"),
        "prerequisites": kp.get("prerequisites", []),
        "source": kp.get("source", ""),
    }


def main():
    vdir = os.path.join(ROOT, "volumes")
    done_vols = []
    for dirpath, _, names in os.walk(vdir):
        for n in names:
            if not n.endswith(".json"):
                continue
            d = load(os.path.join(dirpath, n))
            if d.get("status") == "done":
                done_vols.append(d)

    done_vols.sort(key=lambda v: (GRADE_ORDER.get(v["grade"], 99),
                                  0 if v.get("term") in ("s1", "full") else 1,
                                  v["subject"]))

    topics = []
    all_ids = set()
    for vol in done_vols:
        for kp in vol.get("knowledge_points", []):
            kid = kp.get("id", "")
            if kid in all_ids:
                print(f"[警告] 重复知识点ID跳过: {kid}")
                continue
            all_ids.add(kid)
            topics.append(kp_to_topic(vol, kp))

    out = os.path.join(ROOT, "output", "knowledge_points.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for t in topics:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # 校验所有先修引用
    dangling = 0
    for t in topics:
        for pre in t.get("prerequisites", []):
            if pre not in all_ids:
                dangling += 1

    by_subject = Counter(t["subject"] for t in topics)
    by_domain = Counter(t["domain"] for t in topics)
    print(f"合并完成: {len(done_vols)} 册 -> {len(topics)} 个知识点")
    print(f"输出: {out}")
    print(f"分科: {dict(by_subject)}")
    print(f"领域TOP: {dict(by_domain.most_common(10))}")
    if dangling:
        print(f"[提示] {dangling} 条先修引用指向未梳理的知识点（不影响生成，可用于后续补全）")


if __name__ == "__main__":
    main()
