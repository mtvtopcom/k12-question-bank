#!/usr/bin/env python3
"""步骤1：清洗知识点。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from cleaner import run

if __name__ == "__main__":
    stats = run()
    print("\n分流统计:", stats.get("engine_split"))
    print("科目分布:", stats.get("subject_top"))
