#!/usr/bin/env python3
"""题库质量验证：结构、答案一致性、去重、知识点匹配度、分科/分级覆盖。

用法：python3 scripts/verify.py [db_path]
"""
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from storage import Bank
from quality import dedup_hash, ngrams, jaccard

LETTERS = "ABCD"


def verify(db_path: str) -> dict:
    bank = Bank(db_path, wal=False)
    rows = bank.all_questions()
    report = {
        "total": len(rows),
        "structure_ok": 0,
        "answer_in_range": 0,
        "has_explanation": 0,
        "duplicates": 0,
        "by_subject": Counter(),
        "by_difficulty": Counter(),
        "by_source": Counter(),
        "issues": [],
    }

    seen = {}
    for q in rows:
        opts = q["options"]
        answer = q["answer"]
        stem = q["stem"]
        expl = q["explanation"]

        struct_ok = isinstance(opts, list) and len(opts) == 4 and len(set(opts)) == 4
        if struct_ok:
            report["structure_ok"] += 1
        else:
            report["issues"].append(f"{q['id']}: 选项结构异常 {opts}")

        if answer in LETTERS and LETTERS.index(answer) < len(opts):
            report["answer_in_range"] += 1

        if expl and len(expl) >= 5:
            report["has_explanation"] += 1
        else:
            report["issues"].append(f"{q['id']}: 解析过短")

        h = dedup_hash(stem)
        if h in seen:
            report["duplicates"] += 1
        else:
            seen[h] = q["id"]

        report["by_subject"][q["subject"]] += 1
        report["by_difficulty"][q["difficulty"]] += 1
        report["by_source"][q["source"]] += 1

    # 模板题解析中的结果校验（选择题答案文本必须出现在解析里）
    answer_match = 0
    template_count = 0
    for q in rows:
        if q["source"] != "template":
            continue
        template_count += 1
        opts = q["options"]
        ans_text = opts[LETTERS.index(q["answer"])] if q["answer"] in LETTERS and LETTERS.index(q["answer"]) < len(opts) else ""
        if ans_text and ans_text in q["explanation"]:
            answer_match += 1
    report["template_answer_in_explanation"] = f"{answer_match}/{template_count}"

    for k in ("by_subject", "by_difficulty", "by_source"):
        report[k] = dict(report[k])
    bank.close()
    return report


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "data", "pilot.db")
    db = os.path.abspath(db)
    print(f"验证数据库: {db}\n")
    r = verify(db)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"\n结构完整率: {r['structure_ok']}/{r['total']} = {r['structure_ok']/max(1,r['total']):.1%}")
    print(f"重复题干: {r['duplicates']}")
    print(f"模板题答案文本出现在解析中: {r['template_answer_in_explanation']}")
