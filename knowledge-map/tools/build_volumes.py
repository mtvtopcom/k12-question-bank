#!/usr/bin/env python3
"""从 knowledge-map/data/*.py 的紧凑定义，展开生成标准册 JSON。

这样做的价值：
  - 知识点内容（13 个字段）只写一遍，避免手写 63 册重复结构出错
  - id / index 自动编号，永不越界
  - prerequisites 支持「本册序号」引用，自动转成完整 ID，杜绝悬空引用
  - 每次生成都跑一遍全量校验，字段缺失/领域非法/ID 重复立即报错

数据文件格式（knowledge-map/data/<任意名>.py）：

    VOLUMES = {
        "primary-g1-s1-math": {
            "title": "一年级上册数学",
            "stage": "primary", "grade": "g1", "term": "s1",
            "subject": "math", "school_stage": "S1",
            "source_basis": "人教版/北师大版/苏教版一年级上册；2022版数学课标第一学段",
            "rows": [
                # (domain, theme, unit, name, objective, difficulty, bloom,
                #  prereq, concept, mistakes, types, source, engine)
                ("数与代数", "数与运算", "10以内数的认识", "数一数：1-10各数的认识",
                 "能正确认、读、写1-10各数，理解基数含义", 1, "记忆", [],
                 "数是表示数量的符号，每个数对应相应数量的物体",
                 ["把6和9写反", "数数时漏数或重复"],
                 ["识别", "数数", "选择"], "人教一上·准备课；北师大一上·生活中的数", "template"),
            ],
        },
    }

prereq 中：
    整数   → 本册第 N 个知识点（自动转成 `{volume_id}-K00N`）
    字符串 → 跨册引用，原样保留（必须真实存在，否则校验报错）
    []     → 无前置

用法：
    python3 knowledge-map/tools/build_volumes.py             # 生成全部并校验
    python3 knowledge-map/tools/build_volumes.py --dry-run   # 只校验，不写文件
    python3 knowledge-map/tools/build_volumes.py --only primary-g1-s1-math
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KM_ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(KM_ROOT, "data")
VOL_DIR = os.path.join(KM_ROOT, "volumes")
STANDARDS = os.path.join(KM_ROOT, "taxonomy", "standards.json")
VOLUMES_META = os.path.join(KM_ROOT, "taxonomy", "volumes.json")

# 元组字段顺序，必须与数据文件里的 rows 严格一致
FIELDS = [
    "domain", "theme", "unit", "name", "objective", "difficulty", "bloom",
    "prereq", "concept", "mistakes", "types", "source", "engine",
]

REQUIRED_STR = ("domain", "theme", "unit", "name", "objective", "concept", "source")
VALID_ENGINE = {"template", "llm"}

# 难度锚点：1基础识记 / 2理解应用 / 3分析综合 / 4挑战探究
# 难度描述认知负荷而非年级高低，须与 bloom 保持单调一致
DIFFICULTY_BAND = {
    "记忆": (1, 2),
    "理解": (1, 3),
    "应用": (2, 3),
    "分析": (3, 4),
    "评价": (3, 4),
    "创造": (3, 4),
}


def load_data_files() -> dict:
    """加载 data/*.py 里所有 VOLUMES 定义。"""
    if not os.path.isdir(DATA_DIR):
        sys.exit(f"[错误] 数据目录不存在: {DATA_DIR}")

    all_volumes: dict = {}
    origin: dict = {}
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.py")))
    if not files:
        sys.exit(f"[错误] {DATA_DIR} 下没有任何 .py 数据文件")

    for path in files:
        mod_name = "kmdata_" + os.path.basename(path)[:-3]
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            sys.exit(f"[错误] 无法加载 {path}")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"[错误] 执行 {path} 失败: {exc}")

        volumes = getattr(mod, "VOLUMES", None)
        if not isinstance(volumes, dict):
            sys.exit(f"[错误] {path} 缺少 VOLUMES 字典")

        for vid, vol in volumes.items():
            if vid in all_volumes:
                sys.exit(f"[错误] 册次重复定义: {vid}（{origin[vid]} 与 {path}）")
            all_volumes[vid] = vol
            origin[vid] = os.path.basename(path)

    return all_volumes


def get_valid_domains(standards: dict, stage: str, subject: str) -> set:
    """按学段+学科返回合法的 domain 取值。幼儿园用《指南》五大领域。"""
    if stage == "kg":
        return {"健康", "语言", "社会", "科学", "艺术"}
    return set(standards["domains"][subject]["items"])


def get_valid_themes(standards: dict, school_stage: str, subject: str) -> set:
    """按学段返回合法的主题取值（课标主题与学段严格对应）。

    - 数学： themes_by_stage 中该学段的全部主题（2022课标表2）
    - 语文： 六大任务群（不区分学段）
    - 英语： 六要素 + 三主题范畴 + 语言知识/语言技能子项
    - 幼儿园：五大领域下的子领域无需细分，theme 可空，不校验
    """
    if school_stage == "S0":  # 幼儿园：theme 用《指南》子领域，不按学科课标校验
        return set()
    if subject == "math":
        themes = standards["domains"]["math"]["themes_by_stage"]
        return {t for dom in themes.values() for t in dom.get(school_stage, [])}
    if subject == "chinese":
        # 语文任务群是宏观组织方式（贯穿全学段），知识点粒度允许用细分教学主题
        # （散文阅读/文言文阅读/写作等），故不设严格白名单，仅要求非空（见校验循环）
        return set()
    if subject == "english":
        en = standards["domains"]["english"]
        return (set(en["items"]) | set(en["themes"]) |
                set(en["language_knowledge"]) | set(en["language_skills"]) |
                {"综合技能"})  # 听/说/读/看/写 的综合运用
    return set()  # kg 不校验 theme


def expand_volume(vid: str, vol: dict, errors: list) -> dict:
    """把紧凑定义展开成标准册 JSON。"""
    rows = vol.get("rows") or []
    if not rows:
        errors.append(f"{vid}: rows 为空")
        return {}

    kps = []
    total = len(rows)
    for i, row in enumerate(rows, 1):
        if len(row) != len(FIELDS):
            errors.append(f"{vid} 第{i}条: 字段数 {len(row)} != 期望 {len(FIELDS)}")
            continue
        r = dict(zip(FIELDS, row))

        # 必填字符串校验
        for f in REQUIRED_STR:
            if not isinstance(r[f], str) or not r[f].strip():
                errors.append(f"{vid} 第{i}条: 字段 {f} 为空或非字符串")

        # 难度 / 认知层级校验
        if r["difficulty"] not in (1, 2, 3, 4):
            errors.append(f"{vid} 第{i}条: difficulty={r['difficulty']} 必须是 1-4")
        if r["engine"] not in VALID_ENGINE:
            errors.append(f"{vid} 第{i}条: engine_hint={r['engine']} 必须是 template/llm")

        # 前置关系：整数转本册 ID
        prereqs = []
        for p in (r["prereq"] or []):
            if isinstance(p, int):
                if p < 1 or p > total:
                    errors.append(f"{vid} 第{i}条: prereq 序号 {p} 越界（本册共 {total} 条）")
                    continue
                if p >= i:
                    errors.append(f"{vid} 第{i}条: prereq 序号 {p} 不能指向自身或后续知识点")
                    continue
                prereqs.append(f"{vid}-K{p:03d}")
            elif isinstance(p, str) and p.strip():
                prereqs.append(p.strip())
            else:
                errors.append(f"{vid} 第{i}条: prereq 含非法值 {p!r}")

        kps.append({
            "id": f"{vid}-K{i:03d}",
            "index": i,
            "domain": r["domain"],
            "theme": r["theme"],
            "unit": r["unit"],
            "name": r["name"],
            "objective": r["objective"],
            "difficulty": r["difficulty"],
            "bloom": r["bloom"],
            "prerequisites": prereqs,
            "concept": r["concept"],
            "common_mistakes": list(r["mistakes"] or []),
            "suggested_types": list(r["types"] or []),
            "grade_range": list(vol.get("grade_range") or [vol["grade"]]),
            "source": r["source"],
            "engine_hint": r["engine"],
        })

    return {
        "volume_id": vid,
        "title": vol["title"],
        "stage": vol["stage"],
        "grade": vol["grade"],
        "term": vol["term"],
        "subject": vol["subject"],
        "school_stage": vol["school_stage"],
        "status": "done",
        "source_basis": vol["source_basis"],
        "knowledge_points": kps,
    }


def validate_all(expanded: dict, standards: dict, meta: dict, errors: list) -> dict:
    """全量校验：领域合法性、ID 唯一、跨册前置引用存在、名称册内不重复。"""
    meta_by_id = {v["volume_id"]: v for v in meta["volumes"]}
    all_ids = set()
    for doc in expanded.values():
        for kp in doc["knowledge_points"]:
            all_ids.add(kp["id"])

    stats = {"volumes": len(expanded), "kps": 0, "by_subject": {}, "by_stage": {}}

    for vid, doc in expanded.items():
        m = meta_by_id.get(vid)
        if m is None:
            errors.append(f"{vid}: 不在 taxonomy/volumes.json 清单中")
            continue
        # 元数据一致性
        for key in ("stage", "grade", "term", "subject", "school_stage"):
            if doc[key] != m[key]:
                errors.append(f"{vid}: {key}={doc[key]!r} 与清单 {m[key]!r} 不一致")

        valid_domains = get_valid_domains(standards, doc["stage"], doc["subject"])
        valid_themes = get_valid_themes(standards, doc["school_stage"], doc["subject"])
        names = {}
        for kp in doc["knowledge_points"]:
            if kp["domain"] not in valid_domains:
                errors.append(
                    f"{vid} {kp['id']}: domain={kp['domain']!r} 不在合法集合 {sorted(valid_domains)}"
                )
            if valid_themes and kp.get("theme") and kp["theme"] not in valid_themes:
                errors.append(
                    f"{vid} {kp['id']}: theme={kp['theme']!r} 与学段 {doc['school_stage']} 不符 "
                    f"（合法主题示例 {sorted(valid_themes)[:6]}）"
                )
            if kp["bloom"] not in standards["bloom"]:
                errors.append(f"{vid} {kp['id']}: bloom={kp['bloom']!r} 非法")
            band = DIFFICULTY_BAND.get(kp["bloom"])
            if band and not band[0] <= kp["difficulty"] <= band[1]:
                errors.append(
                    f"{vid} {kp['id']}: difficulty={kp['difficulty']} 与认知层级"
                    f"「{kp['bloom']}」不匹配（应取 {band[0]}-{band[1]}）"
                )
            if kp["name"] in names:
                errors.append(f"{vid}: 知识点名称重复「{kp['name']}」（K{names[kp['name']]:03d} 与 {kp['id']}）")
            names[kp["name"]] = kp["index"]

            for p in kp["prerequisites"]:
                if p not in all_ids:
                    errors.append(f"{vid} {kp['id']}: 前置引用不存在 {p}")

        stats["kps"] += len(doc["knowledge_points"])
        stats["by_subject"][doc["subject"]] = stats["by_subject"].get(doc["subject"], 0) + len(doc["knowledge_points"])
        stats["by_stage"][doc["stage"]] = stats["by_stage"].get(doc["stage"], 0) + len(doc["knowledge_points"])

    # 份量偏离检查（提醒，不阻断）
    for vid, doc in expanded.items():
        m = meta_by_id[vid]
        expect = m["recommended_kp"]
        actual = len(doc["knowledge_points"])
        if abs(actual - expect) > max(3, expect // 3):
            print(f"  [提示] {vid}: {actual} 条，建议 {expect} 条，偏离较大")

    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只校验，不写文件")
    ap.add_argument("--only", default=None, help="只生成指定 volume_id")
    args = ap.parse_args()

    standards = json.load(open(STANDARDS, encoding="utf-8"))
    meta = json.load(open(VOLUMES_META, encoding="utf-8"))
    volumes = load_data_files()

    if args.only:
        if args.only not in volumes:
            sys.exit(f"[错误] 未定义 {args.only}")
        volumes = {args.only: volumes[args.only]}

    errors: list = []
    expanded = {}
    for vid, vol in volumes.items():
        doc = expand_volume(vid, vol, errors)
        if doc:
            expanded[vid] = doc

    stats = validate_all(expanded, standards, meta, errors)

    if errors:
        print(f"\n[校验失败] 共 {len(errors)} 个问题：")
        for e in errors[:60]:
            print("  ✗", e)
        if len(errors) > 60:
            print(f"  ... 另有 {len(errors) - 60} 条")
        sys.exit(1)

    print(f"[校验通过] {stats['volumes']} 册 / {stats['kps']} 个知识点")
    print(f"  分科: {stats['by_subject']}")
    print(f"  分学段: {stats['by_stage']}")

    if args.dry_run:
        print("  （dry-run，未写文件）")
        return

    written = 0
    for vid, doc in expanded.items():
        out_path = os.path.join(VOL_DIR, doc["stage"], doc["subject"], vid + ".json")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        written += 1
    print(f"[已写入] {written} 个册文件 → {VOL_DIR}")

    total_expected = len(meta["volumes"])
    if not args.only and stats["volumes"] < total_expected:
        print(f"\n[进度] 已定义 {stats['volumes']} / {total_expected} 册，还差 {total_expected - stats['volumes']} 册")


if __name__ == "__main__":
    main()
