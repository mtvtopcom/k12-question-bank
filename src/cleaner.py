#!/usr/bin/env python3
"""知识点清洗与分流。

从参考项目读取 output/**/*.json 与 knowledge_graph_complete.json：
1. 过滤空壳（explanation 过短）
2. 过滤串科脏数据（如 subject=语文 但 topic 含数学关键词）
3. 去重（grade+subject+topic）
4. 按 subject + 关键词分流到 template / llm
输出 data/topics.jsonl
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Set, Tuple

from config import PROJECT_ROOT, get_config


def stable_id(prefix: str, key: str, digits: int = 12) -> str:
    """稳定哈希：跨进程一致，保证 topic_id 幂等。"""
    h = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return f"{prefix}_{int(h[:15], 16) % (10 ** digits):0{digits}d}"


CROSS_SUBJECT_SIGNALS = {
    "数学": ["分数乘法", "分数除法", "百分数", "扇形统计图", "数学广角", "位置与方向",
           "圆", "比和比例", "方程", "几何", "应用：", "计算："],
}

FORCE_LLM_DEFAULT = ["阅读", "作文", "古诗", "文言文", "写作", "赏析", "句子赏析", "段落分析"]


def is_cross_subject(subject: str, topic: str, confidence: bool) -> bool:
    """检测串科：非数学科目里出现明显的数学主题词。"""
    if not confidence:
        return False
    if subject == "数学":
        return False
    for sig in CROSS_SUBJECT_SIGNALS["数学"]:
        if sig in topic:
            return True
    return False


def decide_engine(subject: str, topic: str, cfg: Dict[str, Any]) -> str:
    routing = cfg.get("routing", {})
    by_subject = routing.get("engine_by_subject", {}) or {}
    force_words = routing.get("force_llm_keywords", FORCE_LLM_DEFAULT) or []
    engine = by_subject.get(subject, by_subject.get("default", "llm"))
    for w in force_words:
        if w in topic:
            return "llm"
    return str(engine)


def clean_from_output(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    src_cfg = cfg.get("source", {})
    repo = src_cfg.get("repo_path", "/tmp/learning-content-ref")
    min_len = int(src_cfg.get("min_explanation_len", 60))
    drop_cross = bool(src_cfg.get("drop_cross_subject", True))

    pattern = os.path.join(repo, "output", "**", "*.json")
    files = glob.glob(pattern, recursive=True)
    stats = {"total": len(files), "empty": 0, "cross": 0, "dupe": 0, "kept": 0}
    seen: Set[str] = set()
    topics: List[Dict[str, Any]] = []

    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        grade = str(d.get("grade", "")).strip()
        subject = str(d.get("subject", "")).strip()
        topic = str(d.get("topic", "")).strip()
        explanation = str(d.get("explanation", "")).strip()
        chapter = str(d.get("chapter", "")).strip()
        if not (grade and subject and topic):
            continue
        if len(explanation) < min_len:
            stats["empty"] += 1
            continue
        if drop_cross and is_cross_subject(subject, topic, True):
            stats["cross"] += 1
            continue
        key = f"{grade}|{subject}|{topic}"
        if key in seen:
            stats["dupe"] += 1
            continue
        seen.add(key)

        slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_", f"{grade}_{subject}_{topic}")[:60]
        topics.append({
            "topic_id": f"{stable_id('t', key)}_{slug}",
            "grade": grade,
            "subject": subject,
            "topic": topic,
            "chapter": chapter,
            "explanation": explanation[:1200],
            "difficulty_hint": str(d.get("difficulty", "基础")),
            "engine": decide_engine(subject, topic, cfg),
        })

    stats["kept"] = len(topics)
    print(f"[清洗 output] 总文件 {stats['total']} | 空壳 {stats['empty']} | 串科 {stats['cross']} | 重复 {stats['dupe']} | 保留 {stats['kept']}")
    return topics, stats


def clean_from_graph(cfg: Dict[str, Any], existing: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """知识图谱补充：只纳入 output 未覆盖的 grade/subject/topic。"""
    src_cfg = cfg.get("source", {})
    if not src_cfg.get("use_graph", True):
        return existing
    repo = src_cfg.get("repo_path", "/tmp/learning-content-ref")
    gf = os.path.join(repo, "knowledge_graph_complete.json")
    if not os.path.exists(gf):
        return existing
    try:
        with open(gf, "r", encoding="utf-8") as f:
            nodes = json.load(f)
    except Exception:
        return existing

    seen = {(t["grade"], t["subject"], t["topic"]) for t in existing}
    added = 0
    for n in nodes:
        if not isinstance(n, dict):
            continue
        grade = str(n.get("grade", "")).strip()
        subject = str(n.get("subject", "")).strip()
        topic = str(n.get("topic", "")).strip()
        if not (grade and subject and topic):
            continue
        if (grade, subject, topic) in seen:
            continue
        # 图谱里的 topic 常是"教材描述"，过滤掉
        if "课程标准" in topic or "义务教育教科书" in topic:
            continue
        seen.add((grade, subject, topic))
        slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_", f"{grade}_{subject}_{topic}")[:60]
        existing.append({
            "topic_id": f"{stable_id('g', (grade, subject, topic))}_{slug}",
            "grade": grade,
            "subject": subject,
            "topic": topic,
            "chapter": str(n.get("chapter", "")),
            "explanation": str(n.get("explanation", ""))[:1200],
            "difficulty_hint": "基础",
            "engine": decide_engine(subject, topic, cfg),
        })
        added += 1
    print(f"[清洗 图谱] 补充 {added} 个知识点，合计 {len(existing)}")
    return existing


def run() -> Dict[str, Any]:
    cfg = get_config()
    topics, stats = clean_from_output(cfg)
    topics = clean_from_graph(cfg, topics)

    out_path = os.path.join(PROJECT_ROOT, "data", "topics.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for t in topics:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # 分流统计
    from collections import Counter
    eng = Counter(t["engine"] for t in topics)
    subj = Counter(t["subject"] for t in topics)
    stats["engine_split"] = dict(eng)
    stats["subject_top"] = dict(subj.most_common(12))
    stats["output"] = out_path
    stats["total_topics"] = len(topics)
    print(f"[分流] 模板 {eng.get('template',0)} | LLM {eng.get('llm',0)}")
    print(f"[输出] {out_path}")
    return stats


if __name__ == "__main__":
    run()
