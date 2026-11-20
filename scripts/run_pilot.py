#!/usr/bin/env python3
"""步骤2：数学 500 题试点（纯模板引擎，无需 API key）。

用独立的 pilot.db，不污染全量题库。
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from config import get_config
from pipeline import Pipeline

if __name__ == "__main__":
    cfg = get_config()
    # 试点用独立数据库
    cfg["storage"]["db_path"] = "data/pilot.db"

    pipe = Pipeline(cfg, resume=False, verbose=True)
    topics = pipe.load_topics(subject_filter="数学", limit=20)
    print(f"[试点] 数学知识点 {len(topics)} 个，每知识点 25 题，目标 {len(topics)*25} 题\n")

    out_dir = os.path.join(os.path.dirname(HERE), "output", "pilot")
    stats = pipe.run(topics, per_topic=25, out_dir=out_dir,
                     exporters=cfg.get("exporters", ["jsonl", "app_json", "csv"]))

    print("\n" + "=" * 50)
    print("试点完成，统计：")
    print(f"  总题量: {stats['total']}")
    print(f"  按科目: {stats['by_subject']}")
    print(f"  按来源: {stats['by_source']}")
    print(f"  本次拒绝: {stats['rejected_this_run']}")
    print(f"  导出结果: {stats['export']}")
    print(f"  耗时: {stats['elapsed_sec']}s")
    pipe.close()
