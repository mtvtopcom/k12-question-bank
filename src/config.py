#!/usr/bin/env python3
"""配置加载：解析 YAML 的一个安全子集（无第三方依赖）。

支持：嵌套 map、list、标量（bool/int/float/str）、行内 list [a, b]、# 注释。
对于本项目 config.yaml 的复杂度足够。若需要完整 YAML 可换 pyyaml。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "":
        return ""
    if s.lower() in ("true", "yes", "on"):
        return True
    if s.lower() in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(x) for x in re.findall(r"(?:[^,\[\]]|\[[^\]]*\])+", inner)]
    # 去掉行内注释（简单处理：# 前有空格）
    s = re.sub(r"\s+#.*$", "", s).strip()
    return s


def _strip_comment(line: str) -> str:
    # 保留引号内的 #（本项目无此情况，简化处理）
    idx = line.find("#")
    if idx >= 0 and (idx == 0 or line[idx - 1].isspace()):
        line = line[:idx]
    return line.rstrip()


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()

    # 先清理成 (indent, content) 列表
    cleaned = []
    for raw in raw_lines:
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        cleaned.append((indent, line.strip()))

    root: Dict[str, Any] = {}
    # stack of (indent, container)
    stack = [(-1, root)]

    def pop_to(indent: int):
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

    i = 0
    while i < len(cleaned):
        indent, content = cleaned[i]
        if content.startswith("- "):
            pop_to(indent)
            parent = stack[-1][1]
            if isinstance(parent, list):
                parent.append(_parse_scalar(content[2:].strip()))
            i += 1
            continue

        if ":" not in content:
            i += 1
            continue
        key, _, value = content.partition(":")
        key = key.strip()
        value = value.strip()
        pop_to(indent)
        parent = stack[-1][1]

        if value != "":
            if isinstance(parent, dict):
                parent[key] = _parse_scalar(value)
            i += 1
            continue

        # value 为空：向前看，判断子项是 list 还是 dict
        child_is_list = False
        for j in range(i + 1, len(cleaned)):
            nxt_indent, nxt_content = cleaned[j]
            if nxt_indent <= indent:
                break
            if nxt_content.startswith("- "):
                child_is_list = True
            break

        if child_is_list:
            new_container: Any = []
        else:
            new_container = {}
        if isinstance(parent, dict):
            parent[key] = new_container
        stack.append((indent, new_container))
        i += 1

    return root


def resolve_path(project_root: str, rel: str) -> str:
    if os.path.isabs(rel):
        return rel
    return os.path.join(project_root, rel)


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "config.yaml")


def get_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    return load_yaml(path)
