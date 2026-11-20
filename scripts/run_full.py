#!/usr/bin/env python3
"""全量生成：从自建知识点地图扩展到目标题量。支持 --resume 断点续传。

数据源固定为 knowledge-map 的输出，不依赖任何外部项目。

用法：
  python3 knowledge-map/tools/merge.py           # 先合并知识点地图
  export DEEPSEEK_API_KEY=sk-xxx                  # 语义类题目需要
  python3 scripts/run_full.py --target 100000 --resume
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
    ap.add_argument("--target", type=int, default=100000, help="目标题量")
    ap.add_argument("--resume", action="store_true", help="断点续传，跳过已完成知识点")
    ap.add_argument("--subject", default=None, help="只生成某个科目（math/chinese/english）")
    ap.add_argument("--per-topic", type=int, default=0, help="覆盖每知识点配额")
    ap.add_argument("--no-llm", action="store_true", help="禁用 LLM（只用模板引擎）")
    args = ap.parse_args()

    if not os.path.exists(MAP_TOPICS):
        print(f"未找到知识点清单: {MAP_TOPICS}")
        print("请先运行: python3 knowledge-map/tools/merge.py")
        sys.exit(1)

    cfg = get_config()
    if args.no_llm:
        cfg["llm"]["api_key_env"] = "__DISABLED_LLM__"

    pipe = Pipeline(cfg, resume=args.resume, verbose=True)
    topics = pipe.load_topics(path=MAP_TOPICS, subject_filter=args.subject)
    print(f"[全量] 知识点 {len(topics)} 个")

    per_topic = args.per_topic
    if per_topic <= 0:
        per_topic = max(1, round(args.target / max(1, len(topics))))
    print(f"[全量] 每知识点配额 {per_topic} 题，预计 {len(topics)*per_topic} 题")

    out_dir = os.path.join(os.path.dirname(HERE), "output", "full")
    stats = pipe.run(topics, per_topic=per_topic, out_dir=out_dir,
                     exporters=cfg.get("exporters", ["jsonl", "app_json", "csv"]))

    print("\n" + "=" * 50)
    print("全量生成统计：")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    pipe.close()


if __name__ == "__main__":
    main()
