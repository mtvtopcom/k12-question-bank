#!/usr/bin/env python3
"""进度看板：63册梳理进度统计 + 生成下一册梳理提示词。

用法：
  python3 tools/progress.py                    # 查看进度
  python3 tools/progress.py --next 5           # 列出接下来建议梳理的5册
  python3 tools/progress.py --prompt primary-g1-s1-math  # 输出该册的梳理提示词
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def status_map():
    m = {}
    vdir = os.path.join(ROOT, "volumes")
    for dirpath, _, names in os.walk(vdir):
        for n in names:
            if n.endswith(".json"):
                d = load(os.path.join(dirpath, n))
                m[d["volume_id"]] = d
    return m


def print_progress(vols, smap):
    done = doing = todo = 0
    kp_total = 0
    by_stage_subject = defaultdict(lambda: [0, 0])
    for v in vols:
        vid = v["volume_id"]
        d = smap.get(vid, {})
        st = d.get("status", "todo")
        n = len(d.get("knowledge_points", []))
        kp_total += n
        key = (v["stage_name"], v["subject_name"])
        by_stage_subject[key][1] += 1
        if st == "done":
            done += 1
            by_stage_subject[key][0] += 1
        elif st == "doing":
            doing += 1
        else:
            todo += 1

    pct = done / len(vols) * 100 if vols else 0
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    print(f"知识点地图梳理进度: [{bar}] {pct:.1f}%")
    print(f"  已完成 {done} 册 | 进行中 {doing} 册 | 待梳理 {todo} 册 | 共 {len(vols)} 册")
    print(f"  已梳理知识点: {kp_total} 条\n")
    print("分学段·科目:")
    for (stage, subj), (d, t) in sorted(by_stage_subject.items()):
        print(f"  {stage}·{subj}: {d}/{t} 册")


def print_next(vols, smap, k):
    candidates = [v for v in vols if smap.get(v["volume_id"], {}).get("status", "todo") != "done"]
    print(f"接下来建议梳理的 {min(k, len(candidates))} 册：")
    for v in candidates[:k]:
        print(f"  {v['volume_id']:28} {v['title']:14} (建议 {v['recommended_kp']} 个知识点)")


def print_prompt(vid, vols, smap):
    v = next((x for x in vols if x["volume_id"] == vid), None)
    if not v:
        print(f"未找到册次: {vid}")
        return
    domains = {
        "math": "数与代数 / 图形与几何 / 统计与概率 / 综合与实践",
        "chinese": "语言文字积累与梳理 / 实用性阅读与交流 / 文学阅读与创意表达 / 思辨性阅读与表达 / 整本书阅读 / 跨学科学习",
        "english": "主题 / 语篇 / 语言知识 / 文化知识 / 语言技能 / 学习策略",
    }
    print(f"""=== 梳理提示词：{v['title']} ===

请梳理【{v['title']}】的全部知识点，按以下要求：

1. 依据：{v['subject_name']}2022版课程标准 + 主流教材（人教版/统编版）该册目录
2. 数量：约 {v['recommended_kp']} 个知识点，覆盖本册全部单元，不遗漏
3. 领域取值（domain 字段只能选）：{domains.get(v['subject'], '')}
4. 每个知识点按 knowledge-map/SCHEMA.md 的完整字段填写
5. ID 格式：{vid}-K001、K002... 依次编号
6. 前置关系 prerequisites 引用真实存在的知识点ID
7. 输出到文件：knowledge-map/volumes/{v['stage']}/{v['subject']}/{vid}.json
8. 完成后将 status 改为 "done"，source_basis 注明教材版本

完成后运行：
  python3 knowledge-map/tools/validate.py {vid}
  python3 knowledge-map/tools/merge.py""")


def main():
    std = load(os.path.join(ROOT, "taxonomy", "standards.json"))
    vfile = os.path.join(ROOT, "taxonomy", "volumes.json")
    vols = load(vfile)["volumes"]
    smap = status_map()

    args = sys.argv[1:]
    if "--next" in args:
        idx = args.index("--next")
        k = int(args[idx + 1]) if idx + 1 < len(args) else 5
        print_next(vols, smap, k)
    elif "--prompt" in args:
        idx = args.index("--prompt")
        print_prompt(args[idx + 1], vols, smap)
    else:
        print_progress(vols, smap)


if __name__ == "__main__":
    main()
