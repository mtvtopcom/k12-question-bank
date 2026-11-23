#!/usr/bin/env python3
"""桌面管理台的数据访问层。

直接读写 knowledge-map/volumes/**/*.json（下游 merge/validate/pipeline 实际消费的数据）。
所有写操作：写前备份 → 原子替换 → 追加变更日志，支持按操作撤销。
仅用 Python 标准库。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

# ---------------- 路径 ----------------
HERE = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(HERE)                       # desktop/
PROJECT_ROOT = os.path.dirname(APP_ROOT)               # question-bank/
KM_ROOT = os.path.join(PROJECT_ROOT, "knowledge-map")
VOL_ROOT = os.path.join(KM_ROOT, "volumes")
TAXONOMY = os.path.join(KM_ROOT, "taxonomy")
TOOLS = os.path.join(KM_ROOT, "tools")
SRC_ROOT = os.path.join(PROJECT_ROOT, "src")               # 题目生成引擎
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

BACKUP_ROOT = os.path.join(APP_ROOT, "backups")
CHANGELOG = os.path.join(APP_ROOT, "changelog.jsonl")

STAGE_NAMES = {"kg": "幼儿园", "primary": "小学", "junior": "初中"}
SUBJECT_NAMES = {"chinese": "语文", "math": "数学", "english": "英语"}
TERM_NAMES = {"s1": "上册", "s2": "下册"}
GRADE_ORDER = {"k1": 1, "k2": 2, "k3": 3, "g1": 1, "g2": 2, "g3": 3,
               "g4": 4, "g5": 5, "g6": 6, "g7": 7, "g8": 8, "g9": 9}

# 知识点可编辑字段（id/index 为系统字段，不允许改）
EDITABLE = [
    "domain", "theme", "unit", "name", "objective", "difficulty",
    "bloom", "prerequisites", "concept", "common_mistakes",
    "suggested_types", "grade_range", "source", "engine_hint",
]
LIST_FIELDS = {"prerequisites", "common_mistakes", "suggested_types", "grade_range"}


# ---------------- 基础读取 ----------------
def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _grade_label(grade: str) -> str:
    if grade.startswith("g"):
        return f"{int(grade[1:])}年级"
    return {"k1": "小班", "k2": "中班", "k3": "大班"}.get(grade, grade)


def volume_files() -> List[str]:
    out = []
    for dirpath, _, files in os.walk(VOL_ROOT):
        for fn in files:
            if fn.endswith(".json"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def load_all_volumes() -> List[Dict[str, Any]]:
    """返回全部册的元数据 + 知识点，按学段/科目/年级/学期排序。"""
    volumes = []
    for path in volume_files():
        d = _load_json(path)
        d["_path"] = path
        volumes.append(d)

    def key(v):
        return (
            {"kg": 0, "primary": 1, "junior": 2}.get(v["stage"], 9),
            {"chinese": 0, "math": 1, "english": 2}.get(v["subject"], 9),
            GRADE_ORDER.get(v["grade"], 9),
            v["term"],
        )
    volumes.sort(key=key)
    return volumes


def load_taxonomy() -> Dict[str, Any]:
    standards = _load_json(os.path.join(TAXONOMY, "standards.json"))
    return {
        "stage_names": STAGE_NAMES,
        "subject_names": SUBJECT_NAMES,
        "term_names": TERM_NAMES,
        "difficulty": standards.get("difficulty", {}),
        "bloom": standards.get("bloom", []),
        "school_stages": standards.get("school_stages", {}),
        "domains": standards.get("domains", {}),
    }


def stats() -> Dict[str, Any]:
    vols = load_all_volumes()
    total = sum(len(v["knowledge_points"]) for v in vols)
    by_stage: Dict[str, Dict[str, int]] = {}
    for v in vols:
        cell = by_stage.setdefault(v["stage"], {"chinese": 0, "math": 0, "english": 0})
        cell[v["subject"]] = cell.get(v["subject"], 0) + len(v["knowledge_points"])
    return {
        "volumes": len(vols),
        "points": total,
        "by_stage": by_stage,
        "grade_label": _grade_label,
    }


# ---------------- 搜索与筛选 ----------------
def query(q: str = "", stage: str = "", subject: str = "", domain: str = "",
          difficulty: Optional[int] = None, bloom: str = "", engine: str = "",
          limit: int = 0) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """返回 (知识点, 册元数据) 列表。多条件为 AND，q 在多个文本字段中模糊匹配。"""
    q = (q or "").strip().lower()
    out = []
    for v in load_all_volumes():
        if stage and v["stage"] != stage:
            continue
        if subject and v["subject"] != subject:
            continue
        for kp in v["knowledge_points"]:
            if domain and kp.get("domain") != domain:
                continue
            if difficulty and kp.get("difficulty") != difficulty:
                continue
            if bloom and kp.get("bloom") != bloom:
                continue
            if engine and kp.get("engine_hint") != engine:
                continue
            if q:
                hay = " ".join(str(kp.get(f, "")) for f in
                               ("id", "name", "objective", "concept", "domain",
                                "theme", "unit", "source"))
                if q not in hay.lower():
                    continue
            out.append((kp, v))
            if limit and len(out) >= limit:
                return out
    return out


def find_point(topic_id: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for v in load_all_volumes():
        for kp in v["knowledge_points"]:
            if kp["id"] == topic_id:
                return kp, v
    return None


def all_domains() -> List[str]:
    s = set()
    for v in load_all_volumes():
        for kp in v["knowledge_points"]:
            if kp.get("domain"):
                s.add(kp["domain"])
    return sorted(s)


# ---------------- 写操作（备份 → 原子替换 → 日志）----------------
class StoreError(Exception):
    pass


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _backup(path: str, op: str) -> str:
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = os.path.join(BACKUP_ROOT, stamp)
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, os.path.basename(path))
    shutil.copy2(path, dst)
    return dst


def _log(op: str, volume_id: str, topic_id: str, before: Any, after: Any,
         backup_path: str, note: str = "") -> Dict[str, Any]:
    os.makedirs(os.path.dirname(CHANGELOG), exist_ok=True)
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "op": op, "volume_id": volume_id, "topic_id": topic_id,
        "before": before, "after": after, "backup": backup_path, "note": note,
    }
    with open(CHANGELOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _validate_fields(fields: Dict[str, Any]) -> None:
    for k in ("name", "domain", "theme", "unit", "objective", "concept", "source"):
        if not str(fields.get(k, "")).strip():
            raise StoreError(f"字段 {k} 不能为空")
    d = fields.get("difficulty")
    if isinstance(d, str):
        try:
            d = int(d)
        except ValueError:
            raise StoreError("难度必须是 1-4 的整数")
    if d not in (1, 2, 3, 4):
        raise StoreError("难度必须是 1-4 的整数")
    if fields.get("bloom") not in ("记忆", "理解", "应用", "分析", "评价", "创造"):
        raise StoreError("认知层级非法")
    if fields.get("engine_hint") not in ("template", "llm"):
        raise StoreError("引擎提示非法")
    # 难度-认知一致性（与校验器一致）
    band = {"记忆": (1, 2), "理解": (1, 3), "应用": (2, 3),
            "分析": (3, 4), "评价": (3, 4), "创造": (3, 4)}
    lo, hi = band[fields["bloom"]]
    if not lo <= int(d) <= hi:
        raise StoreError(f"难度 {d} 与认知层级「{fields['bloom']}」不匹配（应为 {lo}-{hi}）")


def _normalize(fields: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(fields)
    for k in LIST_FIELDS:
        v = out.get(k, [])
        if isinstance(v, str):
            out[k] = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
        elif not isinstance(v, list):
            out[k] = []
    out["difficulty"] = int(out["difficulty"])
    return out


def update_point(topic_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    found = find_point(topic_id)
    if not found:
        raise StoreError(f"知识点不存在: {topic_id}")
    kp, v = found
    clean = _normalize({k: fields.get(k, kp.get(k)) for k in EDITABLE})
    _validate_fields(clean)
    before = dict(kp)
    backup = _backup(v["_path"], "update")
    doc = _load_json(v["_path"])
    for item in doc["knowledge_points"]:
        if item["id"] == topic_id:
            item.update(clean)
            break
    _atomic_write(v["_path"], doc)
    _log("update", v["volume_id"], topic_id, before, clean, backup)
    return clean


def add_point(volume_id: str, fields: Dict[str, Any]) -> str:
    found = None
    for v in load_all_volumes():
        if v["volume_id"] == volume_id:
            found = v
            break
    if not found:
        raise StoreError(f"册不存在: {volume_id}")
    clean = _normalize(fields)
    _validate_fields(clean)
    doc = _load_json(found["_path"])
    pts = doc["knowledge_points"]
    used = {p["id"] for p in pts}
    idx = 1
    while f"{volume_id}-K{idx:03d}" in used:
        idx += 1
    tid = f"{volume_id}-K{idx:03d}"
    new = {"id": tid, "index": idx}
    for k in EDITABLE:
        new[k] = clean.get(k, "" if k not in LIST_FIELDS else [])
    backup = _backup(found["_path"], "add")
    pts.append(new)
    pts.sort(key=lambda p: p["index"])
    _atomic_write(found["_path"], doc)
    _log("add", volume_id, tid, None, new, backup)
    return tid


def delete_point(topic_id: str) -> None:
    found = find_point(topic_id)
    if not found:
        raise StoreError(f"知识点不存在: {topic_id}")
    kp, v = found
    backup = _backup(v["_path"], "delete")
    doc = _load_json(v["_path"])
    doc["knowledge_points"] = [p for p in doc["knowledge_points"] if p["id"] != topic_id]
    _atomic_write(v["_path"], doc)
    _log("delete", v["volume_id"], topic_id, kp, None, backup)


def changelog(limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(CHANGELOG):
        return []
    with open(CHANGELOG, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return list(reversed(rows))[:limit]


def undo() -> str:
    """撤销最近一次变更，返回被撤销的记录描述。"""
    rows = changelog(limit=1)
    if not rows:
        raise StoreError("没有可撤销的变更")
    rec = rows[0]
    path = None
    for v in load_all_volumes():
        if v["volume_id"] == rec["volume_id"]:
            path = v["_path"]
            break
    if not path:
        raise StoreError("找不到对应册文件，无法撤销")
    backup = rec["backup"]
    if not backup or not os.path.exists(backup):
        raise StoreError("备份文件已丢失，无法自动撤销")
    # 从备份中还原整册（变更前的快照）
    with open(backup, encoding="utf-8") as f:
        old_doc = json.load(f)
    _atomic_write(path, old_doc)
    # 标记该记录已撤销（追加一条 undo 记录，保留审计轨迹）
    with open(CHANGELOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "op": "undo", "volume_id": rec["volume_id"],
            "topic_id": rec["topic_id"], "note": f"撤销 {rec['op']}",
        }, ensure_ascii=False) + "\n")
    return f"已撤销：{rec['op']} {rec['topic_id']}"


# ---------------- 校验与导出 ----------------
def run_checks() -> str:
    """调用现有三套校验脚本，返回合并输出。"""
    scripts = [
        ("结构校验", ["build_volumes.py", "--dry-run"]),
        ("字段校验", ["validate.py"]),
        ("课标交叉校验", ["cross_validate.py"]),
    ]
    chunks = []
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for label, args in scripts:
        try:
            p = subprocess.run(
                [sys.executable, os.path.join(TOOLS, args[0]), *args[1:]],
                cwd=PROJECT_ROOT, capture_output=True, text=True,
                timeout=120, env=env,
            )
            out = (p.stdout or "") + (p.stderr or "")
            status = "通过" if p.returncode == 0 else "发现问题"
            chunks.append(f"===== {label}：{status} =====\n{out.strip()}")
        except Exception as e:
            chunks.append(f"===== {label}：执行失败 =====\n{e}")
    return "\n\n".join(chunks)


def export_csv(rows: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> str:
    cols = ["id", "volume", "stage", "subject", "grade", "index",
            "domain", "theme", "unit", "name", "objective", "difficulty",
            "bloom", "prerequisites", "concept", "common_mistakes",
            "suggested_types", "grade_range", "source", "engine_hint"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for kp, v in rows:
        w.writerow([
            kp.get("id", ""), v["volume_id"], v["stage"], v["subject"], v["grade"],
            kp.get("index", ""), kp.get("domain", ""), kp.get("theme", ""),
            kp.get("unit", ""), kp.get("name", ""), kp.get("objective", ""),
            kp.get("difficulty", ""), kp.get("bloom", ""),
            " | ".join(kp.get("prerequisites", [])), kp.get("concept", ""),
            " | ".join(kp.get("common_mistakes", [])),
            " | ".join(kp.get("suggested_types", [])),
            " | ".join(kp.get("grade_range", [])),
            kp.get("source", ""), kp.get("engine_hint", ""),
        ])
    return buf.getvalue()


def export_json(rows: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> str:
    data = []
    for kp, v in rows:
        item = dict(kp)
        item["volume_id"] = v["volume_id"]
        item["title"] = v["title"]
        data.append(item)
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------- 题目生成（内容感知型引擎）----------------
# 桌面界面直接读写 volumes/*.json（字段名 name/unit/concept/common_mistakes/
# engine_hint，年级是 g3+s2 代码）。生成引擎消费的是 knowledge_points.jsonl
# 格式（topic/chapter/explanation/engine，年级是「三年级下」）。这里做纯内存
# 字段适配，绝不落盘，避免改动正在管理的知识点数据。

_GRADE_CN = {
    "k1": "小班", "k2": "中班", "k3": "大班",
    "g1": "一年级", "g2": "二年级", "g3": "三年级",
    "g4": "四年级", "g5": "五年级", "g6": "六年级",
    "g7": "七年级", "g8": "八年级", "g9": "九年级",
}
_GEN_CFG = {"quality": {
    "min_options": 4, "max_options": 4,
    "require_explanation": True, "min_explanation_chars": 8,
    "dedup_jaccard_threshold": 0.85, "check_answer_in_options": True,
}}

KIND_LABELS = {
    "goal_match": "目标匹配", "statement": "正误辨析", "error_spot": "易错识别",
    "key_term": "核心要领", "prerequisite": "前置知识", "domain_class": "领域归类",
    "percentage": "百分数计算", "equation": "方程求解", "fraction": "分数运算",
    "geometry": "图形计算", "arithmetic": "四则运算",
}


def _grade_cn(grade: str, term: str) -> str:
    """g3 + s2 → 三年级下；k1 → 小班。"""
    base = _GRADE_CN.get(grade, grade)
    if grade.startswith("g"):
        return base + {"s1": "上", "s2": "下"}.get(term, "")
    return base


def _as_generator_topic(kp: Dict[str, Any], v: Dict[str, Any]) -> Dict[str, Any]:
    """单条知识点：volumes 字段 → 生成引擎字段（内存转换）。"""
    mistakes = [str(x).strip() for x in (kp.get("common_mistakes") or []) if str(x).strip()]
    explanation = str(kp.get("concept") or "").strip()
    if mistakes:
        explanation += "\n易错点：" + "；".join(mistakes)
    return {
        "topic_id": kp["id"],
        "topic": kp.get("name", ""),
        "chapter": kp.get("unit", ""),          # volumes.unit = jsonl.chapter
        "grade": _grade_cn(v.get("grade", ""), v.get("term", "")),
        "subject": v.get("subject", ""),
        "domain": kp.get("domain", ""),
        "theme": kp.get("theme", ""),
        "explanation": explanation,
        "objective": kp.get("objective", ""),
        "difficulty": int(kp.get("difficulty", 1) or 1),
        "bloom": kp.get("bloom", "理解"),
        "prerequisites": list(kp.get("prerequisites") or []),
        "engine": kp.get("engine_hint", "template"),
        "source": kp.get("source", ""),
    }


def build_corpus() -> List[Dict[str, Any]]:
    """把当前全部册转成生成引擎输入（每次实时读取，反映界面里的最新编辑）。"""
    out = []
    for v in load_all_volumes():
        for kp in v["knowledge_points"]:
            out.append(_as_generator_topic(kp, v))
    return out


def _make_generator(seed: int, corpus: Optional[List[Dict[str, Any]]] = None):
    from generators.content_aware import ContentAwareGenerator, VERSION  # noqa: F401
    gen = ContentAwareGenerator(int(seed))
    gen.set_corpus(corpus if corpus is not None else build_corpus())
    return gen


def generate_for_point(topic_id: str, n: int = 12, seed: int = 20261120,
                       gate=None) -> List[Dict[str, Any]]:
    """为一个知识点出题，返回通过门禁的题目 dict 列表。"""
    from quality import QualityGate
    found = find_point(topic_id)
    if not found:
        raise StoreError(f"知识点不存在: {topic_id}")
    kp, v = found
    gen = _make_generator(seed)
    topic = _as_generator_topic(kp, v)
    gate = gate or QualityGate(_GEN_CFG)
    out = []
    for q in gen.generate(topic, max(int(n), 0)):
        ok, _ = gate.check(q)
        if ok:
            out.append(q.to_dict())
    return out


def generate_batch(topic_ids: List[str], n: int = 8, seed: int = 20261120,
                   progress=None) -> List[Dict[str, Any]]:
    """批量出题（共享一个门禁做全局去重）。progress(i, total, acc) 回调。"""
    from quality import QualityGate
    corpus = build_corpus()
    by_id = {t["topic_id"]: t for t in corpus}
    gen = _make_generator(seed, corpus)
    gate = QualityGate(_GEN_CFG)
    out: List[Dict[str, Any]] = []
    ids = list(topic_ids)
    for i, tid in enumerate(ids):
        topic = by_id.get(tid)
        if topic:
            for q in gen.generate(topic, max(int(n), 0)):
                ok, _ = gate.check(q)
                if ok:
                    out.append(q.to_dict())
        if progress:
            progress(i + 1, len(ids), len(out))
    return out


def question_kind(q: Dict[str, Any]) -> str:
    """从题干/来源判断题型标签，供界面展示。"""
    text = q.get("stem", "")
    for key in ("学习目标", "哪一课"):
        if key in text:
            return KIND_LABELS["goal_match"]
    for key in ("常见错误", "典型错误", "容易犯", "需要避免", "应避免出现"):
        if key in text:
            return KIND_LABELS["error_spot"]
    for key in ("核心要领", "术语", "核心概念", "关键要领"):
        if key in text:
            return KIND_LABELS["key_term"]
    for key in ("先修", "先掌握", "先具备", "前提", "先铺垫"):
        if key in text:
            return KIND_LABELS["prerequisite"]
    for key in ("归入", "属于下面", "划入", "归属于", "课标"):
        if key in text:
            return KIND_LABELS["domain_class"]
    if any(op in text for op in (" + ", " - ", " × ", "÷", "=", "？")) and \
            any(ch.isdigit() for ch in text):
        return KIND_LABELS["arithmetic"]
    return KIND_LABELS["statement"]


def export_questions(questions: List[Dict[str, Any]], fmt: str) -> str:
    """导出题目：jsonl / csv。"""
    if fmt == "jsonl":
        return "".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions)
    cols = ["id", "topic_id", "grade", "subject", "topic", "chapter",
            "difficulty", "bloom", "type", "stem", "options", "answer",
            "explanation", "source", "generator_version"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for q in questions:
        w.writerow([
            q.get("id", ""), q.get("topic_id", ""), q.get("grade", ""),
            q.get("subject", ""), q.get("topic", ""), q.get("chapter", ""),
            q.get("difficulty", ""), q.get("bloom", ""), q.get("type", ""),
            q.get("stem", ""), " | ".join(q.get("options", [])),
            q.get("answer", ""), q.get("explanation", ""),
            q.get("source", ""), q.get("generator_version", ""),
        ])
    return buf.getvalue()


if __name__ == "__main__":
    # 无界面自测
    s = stats()
    print("册数:", s["volumes"], "知识点:", s["points"])
    print("分学段:", json.dumps(s["by_stage"], ensure_ascii=False))
    r = query("自相矛盾", limit=5)
    print("搜索命中:", len(r), [x[0]["id"] for x in r])
    print("领域数:", len(all_domains()))
    print("选一个:", find_point("primary-g5-s2-chinese-K013")[0]["name"])
