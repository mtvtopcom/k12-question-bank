#!/usr/bin/env python3
"""校验知识点地图：结构完整性、领域合法性、ID唯一性、先修引用有效性。

用法：
  python3 tools/validate.py              # 校验全部册
  python3 tools/validate.py primary-g1-s1-math   # 校验单册
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

REQUIRED_KP_FIELDS = [
    "id", "index", "domain", "unit", "name", "objective", "difficulty",
    "bloom", "prerequisites", "concept", "common_mistakes",
    "suggested_types", "grade_range", "source", "engine_hint",
]
BLOOMS = {"记忆", "理解", "应用", "分析", "评价", "创造"}


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_all_volume_files():
    files = []
    vdir = os.path.join(ROOT, "volumes")
    for dirpath, _, names in os.walk(vdir):
        for n in names:
            if n.endswith(".json"):
                files.append(os.path.join(dirpath, n))
    return sorted(files)


def collect_all_ids(files):
    ids = {}
    for fp in files:
        try:
            d = load(fp)
        except Exception:
            continue
        for kp in d.get("knowledge_points", []):
            if kp.get("id"):
                ids[kp["id"]] = fp
    return ids


def validate_volume(fp, std, all_ids):
    errors = []
    warnings = []
    try:
        d = load(fp)
    except Exception as e:
        return [f"JSON解析失败: {e}"], []

    vid = d.get("volume_id", os.path.basename(fp))
    subject = d.get("subject", "")
    kps = d.get("knowledge_points", [])
    status = d.get("status", "todo")

    if status == "todo" and not kps:
        return [], [f"{vid}: 待梳理（占位）"]

    # 幼儿园阶段统一用《指南》五大领域；义务教育阶段用学科课标领域
    KG_DOMAINS = {"健康", "语言", "社会", "科学", "艺术"}
    if d.get("stage") == "kg":
        domains = set(KG_DOMAINS)
    else:
        domains = set(std.get("domains", {}).get(subject, {}).get("items", []))
        if not domains:
            domains = set(KG_DOMAINS)

    seen_local = set()
    for i, kp in enumerate(kps):
        tag = f"{vid} 第{i+1}条"
        for fld in REQUIRED_KP_FIELDS:
            if fld not in kp:
                errors.append(f"{tag}: 缺字段 {fld}")
        if kp.get("domain") and kp["domain"] not in domains:
            errors.append(f"{tag}: domain「{kp['domain']}」不在合法领域 {sorted(domains)}")
        if kp.get("bloom") and kp["bloom"] not in BLOOMS:
            errors.append(f"{tag}: bloom「{kp['bloom']}」非法")
        if kp.get("difficulty") not in (1, 2, 3, 4):
            errors.append(f"{tag}: difficulty 必须是1-4，实际 {kp.get('difficulty')}")
        kid = kp.get("id", "")
        if kid in seen_local:
            errors.append(f"{tag}: 册内ID重复 {kid}")
        seen_local.add(kid)
        if kid and not kid.startswith(vid + "-K"):
            errors.append(f"{tag}: ID格式错误，应为 {vid}-Kxxx，实际 {kid}")
        for pre in kp.get("prerequisites", []):
            if pre not in all_ids:
                warnings.append(f"{tag}: 前置知识点 {pre} 尚未梳理/不存在")
    return errors, warnings


def main():
    std = load(os.path.join(ROOT, "taxonomy", "standards.json"))
    files = get_all_volume_files()
    all_ids = collect_all_ids(files)
    target = sys.argv[1] if len(sys.argv) > 1 else None

    if target:
        files = [f for f in files if target in os.path.basename(f)]
        if not files:
            print(f"未找到册次: {target}")
            return

    total_err = 0
    total_warn = 0
    done_count = 0
    kp_count = 0
    for fp in files:
        d = load(fp)
        if d.get("status") == "done":
            done_count += 1
            kp_count += len(d.get("knowledge_points", []))
        errors, warnings = validate_volume(fp, std, all_ids)
        total_err += len(errors)
        total_warn += len(warnings)
        for e in errors:
            print("[错误]", e)
        for w in warnings:
            print("[警告]", w)

    print("\n" + "=" * 50)
    print(f"校验册数: {len(files)} | 已完成: {done_count} | 知识点总数: {kp_count}")
    print(f"错误: {total_err} | 警告: {total_warn}")
    if total_err == 0:
        print("✅ 校验通过")
    else:
        print("❌ 存在错误，请修复后重新校验")
        sys.exit(1)


if __name__ == "__main__":
    main()
