#!/usr/bin/env python3
"""从知识点地图生成题目（推荐入口）。

流程：
  1. 先梳理知识点地图（knowledge-map/volumes/**），跑 merge.py 生成 knowledge_points.jsonl
  2. 本脚本读取该文件，注入现有生成管道，题目写入独立数据库 data/map_bank.db

用法：
  python3 scripts/run_from_map.py --try-run          # 用模板引擎小批量试跑（无需API key）
  python3 scripts/run_from_map.py --subject math --per-topic 5
  export DEEPSEEK_API_KEY=sk-xxx
  python3 scripts/run_from_map.py --per-topic 24 --resume   # 全量
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from config import get_config, PROJECT_ROOT
from pipeline import Pipeline

MAP_TOPICS = os.path.join(PROJECT_ROOT, "knowledge-map", "output", "knowledge_points.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-topic", type=int, default=10, help="每知识点题量")
    ap.add_argument("--subject", default=None, help="只生成某学科（math/chinese/english）")
    ap.add_argument("--resume", action="store_true", help="断点续传")
    ap.add_argument("--try-run", action="store_true", help="试跑模式：只用模板引擎+小配额")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个知识点")
    args = ap.parse_args()

    if not os.path.exists(MAP_TOPICS):
        print(f"未找到知识点清单: {MAP_TOPICS}")
        print("请先运行: python3 knowledge-map/tools/merge.py")
        sys.exit(1)

    cfg = get_config()
    cfg["storage"]["db_path"] = "data/map_bank.db"

    if args.try_run:
        # 试跑：禁用 LLM，只跑模板引擎能覆盖的数学/英语知识点
        cfg["llm"]["api_key_env"] = "__DISABLED_LLM__"
        args.subject = args.subject or "math"
        args.per_topic = min(args.per_topic, 6)
        args.limit = args.limit or 10
        print("[试跑模式] 仅模板引擎 + 数学科目 + 小配额")

    pipe = Pipeline(cfg, resume=args.resume, verbose=True)
    topics = pipe.load_topics(path=MAP_TOPICS, subject_filter=args.subject,
                              limit=args.limit or None)
    print(f"[知识点地图] 载入 {len(topics)} 个知识点，每点 {args.per_topic} 题，目标 {len(topics)*args.per_topic} 题\n")

    out_dir = os.path.join(os.path.dirname(HERE), "output", "from_map")
    stats = pipe.run(topics, per_topic=args.per_topic, out_dir=out_dir,
                     exporters=cfg.get("exporters", ["jsonl", "app_json", "csv"]))

    print("\n" + "=" * 50)
    print("生成完成：")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    pipe.close()


if __name__ == "__main__":
    main()
