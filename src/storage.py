#!/usr/bin/env python3
"""存储层：SQLite，负责幂等写入、断点续传、统计。

- 主键用题目 id（确定性哈希），重复插入自动跳过（INSERT OR IGNORE）
- 记录每道题的 source/topic/difficulty，便于按维度统计和补量
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from schema import Question


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    grade TEXT NOT NULL,
    subject TEXT NOT NULL,
    topic TEXT NOT NULL,
    difficulty INTEGER NOT NULL,
    bloom TEXT NOT NULL,
    type TEXT NOT NULL,
    stem TEXT NOT NULL,
    options TEXT NOT NULL,
    answer TEXT NOT NULL,
    explanation TEXT NOT NULL,
    source TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    chapter TEXT NOT NULL DEFAULT '',
    quality TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_topic ON questions(topic_id);
CREATE INDEX IF NOT EXISTS idx_subject ON questions(subject);
CREATE INDEX IF NOT EXISTS idx_grade ON questions(grade);
CREATE INDEX IF NOT EXISTS idx_source ON questions(source);

CREATE TABLE IF NOT EXISTS topics (
    topic_id TEXT PRIMARY KEY,
    grade TEXT, subject TEXT, topic TEXT, engine TEXT,
    target INTEGER DEFAULT 0, done INTEGER DEFAULT 0
);
"""


class Bank:
    def __init__(self, db_path: str, wal: bool = True):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        if wal:
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def upsert_topic(self, topic: Dict[str, Any], target: int) -> None:
        self.conn.execute(
            """INSERT INTO topics(topic_id,grade,subject,topic,engine,target,done)
               VALUES(?,?,?,?,?,?,0)
               ON CONFLICT(topic_id) DO UPDATE SET target=excluded.target""",
            (topic["topic_id"], topic["grade"], topic["subject"], topic["topic"],
             topic.get("engine", "template"), target),
        )
        self.conn.commit()

    def topic_done(self, topic_id: str) -> int:
        row = self.conn.execute("SELECT done FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        return int(row["done"]) if row else 0

    def mark_topic_done(self, topic_id: str, n: int) -> None:
        self.conn.execute("UPDATE topics SET done=done+? WHERE topic_id=?", (n, topic_id))
        self.conn.commit()

    def insert(self, q: Question) -> bool:
        """返回 True 表示新插入，False 表示已存在（幂等跳过）。"""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO questions
               (id,topic_id,grade,subject,topic,difficulty,bloom,type,stem,options,
                answer,explanation,source,generator_version,chapter,quality)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (q.id, q.topic_id, q.grade, q.subject, q.topic, q.difficulty, q.bloom,
             q.type, q.stem, json.dumps(q.options, ensure_ascii=False), q.answer,
             q.explanation, q.source, q.generator_version, q.chapter,
             json.dumps(q.quality.to_dict() if hasattr(q.quality, "to_dict") else q.quality,
                        ensure_ascii=False, default=str)),
        )
        return cur.rowcount > 0

    def insert_many(self, qs: List[Question]) -> int:
        n = 0
        for q in qs:
            if self.insert(q):
                n += 1
        return n

    def count(self, where: str = "", params: tuple = ()) -> int:
        sql = "SELECT COUNT(*) AS c FROM questions"
        if where:
            sql += " WHERE " + where
        return int(self.conn.execute(sql, params).fetchone()["c"])

    def stats(self) -> Dict[str, Any]:
        total = self.count()
        by_subject = {r["subject"]: r["c"] for r in self.conn.execute(
            "SELECT subject, COUNT(*) c FROM questions GROUP BY subject")}
        by_source = {r["source"]: r["c"] for r in self.conn.execute(
            "SELECT source, COUNT(*) c FROM questions GROUP BY source")}
        by_grade = {r["grade"]: r["c"] for r in self.conn.execute(
            "SELECT grade, COUNT(*) c FROM questions GROUP BY grade")}
        return {"total": total, "by_subject": by_subject,
                "by_source": by_source, "by_grade": by_grade}

    def all_questions(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM questions ORDER BY subject,grade,topic,id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = json.loads(d["options"])
            out.append(d)
        return out

    def close(self) -> None:
        self.conn.close()
