#!/usr/bin/env python3
"""根据 standards.json 生成全量册次清单（63 册），并创建待梳理占位文件。

册 = 学段 × 年级 × 册次 × 科目。这是 WorkBuddy 逐册梳理的分治单位。
输出：
  - taxonomy/volumes.json  全量册次清单（含梳理状态）
  - volumes/<stage>/<subject>/<volume_id>.json  每册的知识点文件（首次生成空骨架）
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def school_stage_of(stage: str, grade: str) -> str:
    """返回 S1-S4 学段标签。"""
    if stage == "kg":
        return "S0"
    g = int(grade[1:])
    if g <= 2:
        return "S1"
    if g <= 4:
        return "S2"
    if g <= 6:
        return "S3"
    return "S4"


def volume_code(stage: str, grade: str, term: str, subject: str) -> str:
    return f"{stage}-{grade}-{term}-{subject}"


def build_volumes(std: dict):
    volumes = []
    for stage, sinfo in std["stages"].items():
        for grade in sinfo["grades"]:
            for term in sinfo["terms"]:
                for subject in std["subjects"]:
                    grade_name = sinfo.get("grade_names", {}).get(grade, grade)
                    term_name = sinfo["term_names"][term]
                    subject_name = std["subject_names"][subject]
                    volumes.append({
                        "volume_id": volume_code(stage, grade, term, subject),
                        "stage": stage,
                        "stage_name": sinfo["name"],
                        "grade": grade,
                        "grade_name": grade_name,
                        "term": term,
                        "term_name": term_name,
                        "subject": subject,
                        "subject_name": subject_name,
                        "school_stage": school_stage_of(stage, grade),
                        "title": f"{grade_name}{subject_name}{term_name}",
                        "recommended_kp": std["recommended_kp_per_volume"][stage][subject],
                    })
    return volumes


def empty_volume(vol: dict) -> dict:
    return {
        "volume_id": vol["volume_id"],
        "title": vol["title"],
        "stage": vol["stage"], "grade": vol["grade"], "term": vol["term"],
        "subject": vol["subject"], "school_stage": vol["school_stage"],
        "status": "todo",
        "source_basis": "",
        "knowledge_points": [],
    }


def main():
    std = load(os.path.join(ROOT, "taxonomy", "standards.json"))
    volumes = build_volumes(std)

    vp = os.path.join(ROOT, "taxonomy", "volumes.json")
    with open(vp, "w", encoding="utf-8") as f:
        json.dump({"total": len(volumes), "volumes": volumes}, f, ensure_ascii=False, indent=2)

    created = 0
    for vol in volumes:
        fp = os.path.join(ROOT, "volumes", vol["stage"], vol["subject"], vol["volume_id"] + ".json")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        if not os.path.exists(fp):
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(empty_volume(vol), f, ensure_ascii=False, indent=2)
            created += 1

    print(f"生成册次清单: {len(volumes)} 册 -> {vp}")
    print(f"新建占位文件: {created} 个（已存在的不覆盖）")
    from collections import Counter
    c = Counter((v["stage_name"], v["subject_name"]) for v in volumes)
    print("分布:")
    for k, v in sorted(c.items()):
        print(f"  {k[0]}·{k[1]}: {v} 册")


if __name__ == "__main__":
    main()
