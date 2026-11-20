#!/usr/bin/env python3
"""导出器：JSONL / App JSON / CSV，都从统一 Question 派生。"""
from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from typing import Any, Dict, List

LETTERS = "ABCD"


def _row(d: Dict[str, Any]) -> Dict[str, Any]:
    """统一库行 → 展示行。"""
    opts = d.get("options", [])
    if isinstance(opts, str):
        opts = json.loads(opts)
    answer = d.get("answer", "A")
    answer_text = ""
    if answer in LETTERS and ord(answer) - ord("A") < len(opts):
        answer_text = opts[ord(answer) - ord("A")]
    return {
        "id": d["id"], "grade": d["grade"], "subject": d["subject"],
        "topic": d["topic"], "difficulty": d["difficulty"], "bloom": d["bloom"],
        "stem": d["stem"], "options": opts, "answer": answer,
        "answer_text": answer_text, "explanation": d["explanation"],
        "source": d["source"], "chapter": d.get("chapter", ""),
    }


def export_jsonl(rows: List[Dict[str, Any]], path: str) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for d in rows:
            f.write(json.dumps(_row(d), ensure_ascii=False) + "\n")
    return len(rows)


def export_csv(rows: List[Dict[str, Any]], path: str) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = ["id", "grade", "subject", "topic", "difficulty", "bloom", "stem",
              "options", "answer", "answer_text", "explanation", "source", "chapter"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for d in rows:
            r = _row(d)
            r["options"] = " | ".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(r["options"]))
            w.writerow({k: r.get(k, "") for k in fields})
    return len(rows)


def export_app_json(rows: List[Dict[str, Any]], out_dir: str) -> int:
    """学习 App 导入格式：按 年级/科目/知识点 分目录，一个知识点一个 JSON。"""
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    meta: Dict[str, Dict[str, str]] = {}
    for d in rows:
        key = f"{d['grade']}__{d['subject']}__{d['topic']}"
        grouped[key].append(d)
        meta[key] = {"grade": d["grade"], "subject": d["subject"], "topic": d["topic"],
                     "chapter": d.get("chapter", "")}

    safe = lambda s: "".join(c if c not in '/\\:*?"<>|' else "_" for c in s)[:60]
    written = 0
    for key, items in grouped.items():
        m = meta[key]
        grade_dir = os.path.join(out_dir, safe(m["grade"]), safe(m["subject"]))
        os.makedirs(grade_dir, exist_ok=True)
        practice = []
        for d in items:
            r = _row(d)
            practice.append({
                "id": r["id"],
                "type": "multiple_choice",
                "difficulty": r["difficulty"],
                "question": r["stem"],
                "options": r["options"],
                "answer": r["answer"],
                "answer_text": r["answer_text"],
                "explanation": r["explanation"],
                "feedback": {
                    "correct": "回答正确，继续保持！",
                    "incorrect": f"再想想，正确答案是 {r['answer']}. {r['answer_text']}",
                },
            })
        payload = {
            "id": f"{safe(m['grade'])}_{safe(m['subject'])}_{safe(m['topic'])}",
            "grade": m["grade"], "subject": m["subject"], "topic": m["topic"],
            "chapter": m["chapter"], "practice": practice,
        }
        fp = os.path.join(grade_dir, f"{safe(m['topic'])}.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        written += len(items)
    return written


def export_all(questions: List[Dict[str, Any]], out_dir: str, kinds: List[str]) -> Dict[str, int]:
    os.makedirs(out_dir, exist_ok=True)
    result = {}
    if "jsonl" in kinds:
        p = os.path.join(out_dir, "questions.jsonl")
        result["jsonl"] = export_jsonl(questions, p)
    if "csv" in kinds:
        p = os.path.join(out_dir, "questions.csv")
        result["csv"] = export_csv(questions, p)
    if "app_json" in kinds:
        p = os.path.join(out_dir, "app_import")
        result["app_json"] = export_app_json(questions, p)
    return result
