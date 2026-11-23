#!/usr/bin/env python3
"""桌面管理台的数据访问层。

直接读写 knowledge-map/volumes/**/*.json（下游 merge/validate/pipeline 实际消费的数据）。
所有写操作：写前备份 → 原子替换 → 追加变更日志，支持按操作撤销。
「恢复原始配置」从 desktop/original/ 的出厂快照整体还原（同样先备份再覆盖）。
仅用 Python 标准库。
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
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
# 出厂快照：安装包里附带的 63 册 / 1225 个知识点原始配置
ORIGINAL_DIR = os.path.join(APP_ROOT, "original")
ORIGINAL_ARCHIVE = os.path.join(ORIGINAL_DIR, "library.json.gz")
ORIGINAL_MANIFEST = os.path.join(ORIGINAL_DIR, "manifest.json")

STAGE_NAMES = {"kg": "幼儿园", "primary": "小学", "junior": "初中"}
SUBJECT_NAMES = {"chinese": "语文", "math": "数学", "english": "英语"}
TERM_NAMES = {"s1": "上册", "s2": "下册"}
GRADE_ORDER = {"k1": 1, "k2": 2, "k3": 3, "g1": 1, "g2": 2, "g3": 3,
               "g4": 4, "g5": 5, "g6": 6, "g7": 7, "g8": 8, "g9": 9}

# 知识点可编辑字段（id/index 为系统字段，不允许改）
EDITABLE = [
    "domain", "theme", "unit", "name", "objective", "difficulty",
    "bloom", "prerequisites", "concept", "common_mistakes",
    "suggested_types", "grade_range", "source",
]
LIST_FIELDS = {"prerequisites", "common_mistakes", "suggested_types", "grade_range"}


# ---------------- 基础读取 ----------------
def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _grade_label(grade: str) -> str:
    if grade.startswith("g"):
        cn = {"1": "一", "2": "二", "3": "三", "4": "四",
              "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
        n = str(int(grade[1:]))
        return f"{cn.get(n, n)}年级"
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
          difficulty: Optional[int] = None, bloom: str = "",
          limit: int = 0, grade: str = "") -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """返回 (知识点, 册元数据) 列表。多条件为 AND，q 在多个文本字段中模糊匹配。"""
    q = (q or "").strip().lower()
    out = []
    for v in load_all_volumes():
        if stage and v["stage"] != stage:
            continue
        if subject and v["subject"] != subject:
            continue
        if grade and v["grade"] != grade:
            continue
        for kp in v["knowledge_points"]:
            if domain and kp.get("domain") != domain:
                continue
            if difficulty and kp.get("difficulty") != difficulty:
                continue
            if bloom and kp.get("bloom") != bloom:
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
    if rec["op"] == "restore":
        # 恢复原始配置是整库覆盖，撤销 = 从恢复前的整目录快照再覆盖回来
        src = rec.get("backup") or ""
        if not os.path.isdir(src):
            raise StoreError("恢复前的备份目录已丢失，无法自动撤销")
        count, _ = _apply_tree(src)
        _mark_undo(rec, f"撤销 恢复原始配置（{count} 册）")
        return "已撤销：恢复原始配置（回到恢复前的数据）"
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
    _mark_undo(rec)
    return f"已撤销：{rec['op']} {rec['topic_id']}"


def _mark_undo(rec: Dict[str, Any], note: str = "") -> None:
    """追加一条 undo 记录，保留审计轨迹。"""
    with open(CHANGELOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "op": "undo", "volume_id": rec.get("volume_id", ""),
            "topic_id": rec.get("topic_id", ""),
            "note": note or f"撤销 {rec['op']}",
        }, ensure_ascii=False) + "\n")


# ---------------- 出厂快照与恢复 ----------------
def original_info() -> Optional[Dict[str, Any]]:
    """读取出厂快照清单；没有快照时返回 None。"""
    if not (os.path.exists(ORIGINAL_ARCHIVE) and os.path.exists(ORIGINAL_MANIFEST)):
        return None
    try:
        return _load_json(ORIGINAL_MANIFEST)
    except Exception:
        return None


def create_original_snapshot(version: str = "") -> Dict[str, Any]:
    """把当前 volumes 打包成出厂快照（发布前运行一次）。

    存成单个 gzip JSON（约 150 KB），避免在仓库里再复制 1.2 MB 的册文件；
    清单记录册数、知识点数、SHA-256，恢复时可校验完整性。
    """
    bundle: Dict[str, Any] = {}
    for path in volume_files():
        rel = os.path.relpath(path, VOL_ROOT).replace(os.sep, "/")
        bundle[rel] = _load_json(path)
    payload = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    os.makedirs(ORIGINAL_DIR, exist_ok=True)
    tmp = ORIGINAL_ARCHIVE + ".tmp"
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(payload)
    os.replace(tmp, ORIGINAL_ARCHIVE)

    points = sum(len(d.get("knowledge_points", [])) for d in bundle.values())
    manifest = {
        "version": version,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "volumes": len(bundle),
        "points": points,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "archive": os.path.basename(ORIGINAL_ARCHIVE),
    }
    tmp = ORIGINAL_MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, ORIGINAL_MANIFEST)
    return manifest


def _apply_tree(src_root: str) -> Tuple[int, List[str]]:
    """用 src_root 下的全部 .json 覆盖 VOL_ROOT，并删除快照里没有的册。

    返回 (写入册数, 被删除的多余册相对路径)。
    """
    src_files = []
    for dirpath, _, files in os.walk(src_root):
        for fn in files:
            if fn.endswith(".json"):
                src_files.append(os.path.join(dirpath, fn))
    if not src_files:
        raise StoreError("快照里没有可恢复的册文件")
    keep = set()
    for path in src_files:
        rel = os.path.relpath(path, src_root)
        keep.add(rel.replace(os.sep, "/"))
        dst = os.path.join(VOL_ROOT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".tmp"
        shutil.copy2(path, tmp)
        os.replace(tmp, dst)
    return len(src_files), _prune_extra(keep)


def _prune_extra(keep: set) -> List[str]:
    """删除不在 keep 集合里的册文件（快照之外的额外册）。"""
    removed = []
    for path in volume_files():
        rel = os.path.relpath(path, VOL_ROOT).replace(os.sep, "/")
        if rel not in keep:
            os.remove(path)
            removed.append(rel)
    return removed


def restore_original() -> Dict[str, Any]:
    """把知识点库整体恢复为软件出厂配置（63 册 / 1225 个知识点）。

    先整目录备份当前 volumes，再按快照覆盖，最后清掉快照之外的额外册。
    返回 {volumes, points, removed, backup}。
    """
    if not os.path.exists(ORIGINAL_ARCHIVE):
        raise StoreError("未找到出厂快照（desktop/original/library.json.gz），无法恢复")
    try:
        with gzip.open(ORIGINAL_ARCHIVE, "rb") as f:
            bundle = json.loads(f.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise StoreError(f"出厂快照损坏：{exc}")
    if not isinstance(bundle, dict) or not bundle:
        raise StoreError("出厂快照内容为空，无法恢复")

    # 1) 整目录备份当前数据
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = os.path.join(BACKUP_ROOT, f"restore-{stamp}")
    shutil.copytree(VOL_ROOT, folder)

    # 2) 按快照逐册原子写回
    keep = set()
    for rel, doc in bundle.items():
        keep.add(rel)
        dst = os.path.join(VOL_ROOT, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, dst)

    # 3) 清掉快照之外多出来的册
    removed = _prune_extra(keep)

    points = sum(len(d.get("knowledge_points", [])) for d in bundle.values())
    _log("restore", "-", "", None,
         {"volumes": len(bundle), "points": points, "removed": removed},
         folder, note="恢复原始配置")
    return {"volumes": len(bundle), "points": points,
            "removed": removed, "backup": folder}


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
            "suggested_types", "grade_range", "source"]
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
            kp.get("source", ""),
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
# 桌面界面直接读写 volumes/*.json（字段名 name/unit/concept/common_mistakes，
# 年级是 g3+s2 代码）。生成引擎消费的是 knowledge_points.jsonl 格式
# （topic/chapter/explanation，年级是「三年级下」）。这里做纯内存字段适配，
# 绝不落盘，避免改动正在管理的知识点数据。

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
    "fact_choice": "要点判断", "mistake_spot": "误区识别", "term_cloze": "术语填空",
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
    """从题干判断题型标签，供界面展示。题只考内容，无元数据题型。"""
    text = q.get("stem", "")
    for key in ("空缺", "填入", "补全", "横线", "关键术语", "＿＿"):
        if key in text:
            return KIND_LABELS["term_cloze"]
    for key in ("容易出错", "需要避免", "常见误区", "不恰当", "偏离目标", "最应避免"):
        if key in text:
            return KIND_LABELS["mistake_spot"]
    if any(op in text for op in (" + ", " - ", " × ", "÷", "=", "多少", "?")) and \
            any(ch.isdigit() for ch in text):
        return KIND_LABELS["arithmetic"]
    return KIND_LABELS["fact_choice"]


def build_point_prompt(topic_id: str, n: int = 5,
                       difficulty_mix: Optional[Dict[int, int]] = None,
                       output_format: str = "json") -> str:
    """为单个知识点生成大模型出题提示词（前置 ID 自动补成名称）。"""
    import prompt as prompt_mod
    found = find_point(topic_id)
    if not found:
        raise StoreError(f"知识点不存在: {topic_id}")
    kp, v = found
    titles = {}
    for vol in load_all_volumes():
        for item in vol["knowledge_points"]:
            titles[item["id"]] = item.get("name", item["id"])
    return prompt_mod.build_prompt(kp, v, n, title_by_id=titles,
                                   difficulty_mix=difficulty_mix,
                                   output_format=output_format)


def build_batch_prompt(topic_ids: List[str], n_per: int = 3,
                       difficulty_mix: Optional[Dict[int, int]] = None,
                       output_format: str = "json") -> str:
    """为多个知识点生成一条批量出题提示词。"""
    import prompt as prompt_mod
    titles = {}
    vols = load_all_volumes()
    for vol in vols:
        for item in vol["knowledge_points"]:
            titles[item["id"]] = item.get("name", item["id"])
    index = {kp["id"]: (kp, vol) for vol in vols for kp in vol["knowledge_points"]}
    points = []
    for tid in topic_ids:
        if tid in index:
            kp, vol = index[tid]
            points.append({"kp": kp, "vol": vol})
    if not points:
        raise StoreError("没有可生成提示词的知识点")
    return prompt_mod.build_batch_prompt(points, n_per, title_by_id=titles,
                                         difficulty_mix=difficulty_mix,
                                         output_format=output_format)


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
