#!/usr/bin/env python3
"""API 接入配置：三家协议的预设、用户配置文件与运行时数据目录。

- 配置文件、题库、任务都放在「用户数据目录」，不写进 .app 包内，避免只读或丢数据。
  macOS: ~/Library/Application Support/K12题库工厂/
- 配置文件权限 0600，API Key 只存本机，不写入仓库、日志与导出文件。
- 可用环境变量 K12_DATA_DIR 覆盖数据目录（测试与便携使用）。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

APP_DIR_NAME = "K12题库工厂"
ENV_DATA_DIR = "K12_DATA_DIR"

PROVIDER_LABELS = {
    "openai": "OpenAI 兼容",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
}
PROVIDER_ORDER = ["openai", "anthropic", "gemini"]
LABEL_TO_PROVIDER = {v: k for k, v in PROVIDER_LABELS.items()}

# 预设：(名称, Base URL, 默认模型)。都可在界面上改。
PRESETS: Dict[str, List[Tuple[str, str, str]]] = {
    "openai": [
        ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
        ("通义千问 · DashScope", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
        ("Kimi · Moonshot", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
        ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
        ("豆包 · 火山方舟", "https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k"),
        ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
        ("Ollama 本地", "http://localhost:11434/v1", "qwen2.5:7b"),
        ("自定义", "", ""),
    ],
    "anthropic": [
        ("Claude 官方", "https://api.anthropic.com", "claude-sonnet-4-5"),
        ("自定义", "", ""),
    ],
    "gemini": [
        ("Gemini 官方", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash"),
        ("自定义", "", ""),
    ],
}

DEFAULT_PROFILE: Dict[str, Any] = {
    "preset": "",
    "base_url": "",
    "model": "",
    "api_key": "",
    "temperature": 0.8,
    "max_tokens": 8192,
    "timeout": 90,
    "interval": 0.8,       # 两次请求之间的最小间隔（秒），避免触发限流
    "json_mode": True,     # 支持 JSON 模式的服务会返回更稳定的结构
    "batch": 6,            # 单次请求最多生成几道题
}
DEFAULT_GENERATION: Dict[str, Any] = {
    "n": "5",
    "n_custom": "",
    "preset": "基础为主",
    "ratios": {"1": 50, "2": 30, "3": 15, "4": 5},
}


def data_dir() -> str:
    """用户数据目录（配置 / 题库 / 任务），首次调用时创建。"""
    override = (os.environ.get(ENV_DATA_DIR) or "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", APP_DIR_NAME)
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_DIR_NAME)
    else:
        path = os.path.join(os.path.expanduser("~"), ".config", APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def bank_path() -> str:
    return os.path.join(data_dir(), "questions.jsonl")


def tasks_dir() -> str:
    path = os.path.join(data_dir(), "tasks")
    os.makedirs(path, exist_ok=True)
    return path


def exports_dir() -> str:
    """按任务产出的 JSON 存档目录（文件名 = 任务名）。"""
    path = os.path.join(data_dir(), "exports")
    os.makedirs(path, exist_ok=True)
    return path


def _default_config() -> Dict[str, Any]:
    profiles: Dict[str, Any] = {}
    for provider in PROVIDER_ORDER:
        preset = PRESETS[provider][0]
        profile = dict(DEFAULT_PROFILE)
        profile.update({"preset": preset[0], "base_url": preset[1], "model": preset[2]})
        profiles[provider] = profile
    return {
        "version": 1,
        "provider": "openai",
        "profiles": profiles,
        "generation": json.loads(json.dumps(DEFAULT_GENERATION)),
    }


def _merge_profile(raw: Any) -> Dict[str, Any]:
    profile = dict(DEFAULT_PROFILE)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in profile:
                profile[key] = value
    return profile


def load() -> Dict[str, Any]:
    """读取配置；文件缺失或损坏时回退默认值（损坏文件留作 .bad 备份）。"""
    cfg = _default_config()
    path = config_path()
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001
        try:
            os.replace(path, path + ".bad")
        except OSError:
            pass
        return cfg
    if not isinstance(raw, dict):
        return cfg

    provider = raw.get("provider")
    if provider not in PROVIDER_LABELS:
        provider = "openai"
    cfg["provider"] = provider
    profiles = raw.get("profiles")
    if isinstance(profiles, dict):
        for name in PROVIDER_ORDER:
            merged = _merge_profile(profiles.get(name))
            if not merged.get("base_url") or not merged.get("model"):
                base = cfg["profiles"][name]
                merged["base_url"] = merged.get("base_url") or base["base_url"]
                merged["model"] = merged.get("model") or base["model"]
            cfg["profiles"][name] = merged
    gen = raw.get("generation")
    if isinstance(gen, dict):
        if str(gen.get("n", "")).strip():
            cfg["generation"]["n"] = str(gen.get("n"))
        cfg["generation"]["n_custom"] = str(gen.get("n_custom") or "")
        if isinstance(gen.get("preset"), str):
            cfg["generation"]["preset"] = gen["preset"]
        ratios = gen.get("ratios")
        if isinstance(ratios, dict):
            for d in ("1", "2", "3", "4"):
                try:
                    cfg["generation"]["ratios"][d] = max(0, int(ratios.get(d, 0)))
                except (TypeError, ValueError):
                    pass
    return cfg


def save(cfg: Dict[str, Any]) -> None:
    """原子写入配置，权限 0600（仅当前用户可读写）。"""
    path = config_path()
    payload = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def profile(cfg: Dict[str, Any], provider: Optional[str] = None) -> Dict[str, Any]:
    """取某协议的配置副本，并带上 provider 字段。"""
    name = provider or cfg.get("provider") or "openai"
    if name not in PROVIDER_LABELS:
        name = "openai"
    out = _merge_profile((cfg.get("profiles") or {}).get(name))
    out["provider"] = name
    return out


def preset_names(provider: str) -> List[str]:
    return [p[0] for p in PRESETS.get(provider, [])]


def preset_values(provider: str, name: str) -> Optional[Tuple[str, str]]:
    for label, base, model in PRESETS.get(provider, []):
        if label == name:
            return base, model
    return None


def mask_key(key: str) -> str:
    """日志/界面提示用的脱敏形式。"""
    key = (key or "").strip()
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}…{key[-3:]}"


def summary(cfg: Dict[str, Any]) -> Dict[str, Any]:
    prof = profile(cfg)
    return {"provider": prof["provider"],
            "provider_label": PROVIDER_LABELS.get(prof["provider"], prof["provider"]),
            "model": prof["model"], "base_url": prof["base_url"]}
