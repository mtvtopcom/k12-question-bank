#!/usr/bin/env python3
"""主调度管道：清洗→生成→质检→入库→导出。

支持断点续传：每个知识点按 (done/target) 记录进度，--resume 时跳过已完成的。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from config import PROJECT_ROOT, get_config, resolve_path
from storage import Bank
from quality import QualityGate
from generators.template import TemplateGenerator
from generators.content_aware import ContentAwareGenerator
from generators.llm import LLMGenerator
from generators.providers import build_provider
from exporters import export_all

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class Pipeline:
    def __init__(self, cfg: Dict[str, Any], resume: bool = True, verbose: bool = True):
        self.cfg = cfg
        self.resume = resume
        self.verbose = verbose
        db_path = resolve_path(PROJECT_ROOT, cfg.get("storage", {}).get("db_path", "data/bank.db"))
        self.bank = Bank(db_path, wal=cfg.get("storage", {}).get("wal_mode", True))
        self.gate = QualityGate(cfg)

        tmpl_cfg = cfg.get("template", {})
        seed = int(tmpl_cfg.get("seed", 20261120))
        # v2: 默认用内容感知生成器（读知识点内容出题），旧引擎作为可选回退。
        self.use_content_aware = bool(tmpl_cfg.get("content_aware", True))
        if self.use_content_aware:
            self.template_gen = ContentAwareGenerator(seed=seed)
        else:
            self.template_gen = TemplateGenerator(
                seed=seed, enabled=tmpl_cfg.get("generators", {}),
            )
        llm_cfg = cfg.get("llm", {})
        self.provider = build_provider(cfg)
        self.llm_gen = LLMGenerator(
            seed=int(tmpl_cfg.get("seed", 20261120)),
            provider=self.provider,
            variants=int(llm_cfg.get("variants_per_mother", 8)),
            batch=int(llm_cfg.get("batch_mothers", 1)),
        )

    def log(self, *args):
        if self.verbose:
            print(*args, flush=True)

    def load_topics(self, path: Optional[str] = None, subject_filter: Optional[str] = None,
                    limit: Optional[int] = None) -> List[Dict[str, Any]]:
        path = path or os.path.join(PROJECT_ROOT, "knowledge-map", "output", "knowledge_points.jsonl")
        topics = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                if subject_filter and d["subject"] != subject_filter:
                    continue
                topics.append(d)
        if limit:
            topics = topics[:limit]
        return topics

    def run(self, topics: List[Dict[str, Any]], per_topic: int, out_dir: str,
            exporters: List[str]) -> Dict[str, Any]:
        quota = self.cfg.get("quota", {})
        total_inserted = 0
        total_rejected = 0
        t0 = time.time()

        # 内容感知生成器需要全库语料来抽取跨知识点的干扰项
        if self.use_content_aware:
            try:
                all_topics = self.load_topics()
                self.template_gen.set_corpus(all_topics)
                self.log(f"[内容感知] 已载入全库 {len(all_topics)} 个知识点作为干扰项语料")
            except Exception as e:
                self.log(f"[内容感知] 语料载入失败，退化为单知识点模式: {e}")

        # 预加载已有题干用于跨批次去重
        existing = self.bank.all_questions()
        if existing:
            self.gate.load_existing(existing)
            self.log(f"[去重] 已加载 {len(existing)} 条历史题干")

        for i, topic in enumerate(topics, 1):
            self.bank.upsert_topic(topic, per_topic)
            done = self.bank.topic_done(topic["topic_id"]) if self.resume else 0
            need = per_topic - done
            if need <= 0:
                continue

            engine = topic.get("engine", "template")
            gen_label = engine
            # LLM 引擎若密钥不可用，自动降级到内容感知/模板引擎
            if engine == "llm" and not self.provider.available():
                engine = "template"
                gen_label = "llm→content_aware" if self.use_content_aware else "llm→template"
            elif engine == "template" and self.use_content_aware:
                gen_label = "content_aware"

            gen = self.template_gen if engine == "template" else self.llm_gen
            candidates = gen.generate(topic, need)

            accepted = []
            for q in candidates:
                ok, notes = self.gate.check(q)
                if ok:
                    accepted.append(q)
                else:
                    total_rejected += 1

            n = self.bank.insert_many(accepted)
            if n:
                self.bank.mark_topic_done(topic["topic_id"], n)
            total_inserted += n

            if self.verbose and (i <= 3 or i % 20 == 0):
                self.log(f"[{i}/{len(topics)}] {topic['grade']} {topic['subject']} "
                         f"{topic['topic'][:16]} | {gen_label} | +{n} | 累计 {total_inserted}")

        elapsed = time.time() - t0
        rows = self.bank.all_questions()
        exp = export_all(rows, out_dir, exporters)

        stats = self.bank.stats()
        stats.update({
            "inserted_this_run": total_inserted,
            "rejected_this_run": total_rejected,
            "elapsed_sec": round(elapsed, 1),
            "export": exp,
            "out_dir": out_dir,
        })
        return stats

    def close(self):
        self.bank.close()
