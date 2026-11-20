#!/usr/bin/env python3
"""LLM provider 适配：DeepSeek / Qwen / 豆包 等 OpenAI 兼容接口。

只用标准库 urllib，无第三方依赖。密钥从环境变量读取。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class LLMError(Exception):
    pass


class OpenAICompatProvider:
    def __init__(self, base_url: str, model: str, api_key_env: str,
                 temperature: float = 0.85, max_tokens: int = 2000,
                 timeout: int = 60, max_retries: int = 3, rpm: int = 20):
        self.base_url = base_url
        self.model = model
        self.api_key = os.environ.get(api_key_env, "")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last_call = 0.0

    def available(self) -> bool:
        return bool(self.api_key)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.time()

    def chat(self, system: str, user: str, json_mode: bool = True) -> str:
        if not self.api_key:
            raise LLMError(f"未设置环境变量 {self.__class__.__name__} 所需的 API key")
        self._throttle()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_err = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(self.base_url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}"
                if e.code in (400, 401, 403):
                    break  # 不可重试
                time.sleep(2 ** attempt)
            except Exception as e:
                last_err = str(e)
                time.sleep(2 ** attempt)
        raise LLMError(f"调用失败（重试 {self.max_retries} 次）: {last_err}")


def build_provider(cfg: Dict[str, Any]) -> OpenAICompatProvider:
    c = cfg.get("llm", {})
    return OpenAICompatProvider(
        base_url=c.get("base_url", "https://api.deepseek.com/v1/chat/completions"),
        model=c.get("model", "deepseek-chat"),
        api_key_env=c.get("api_key_env", "DEEPSEEK_API_KEY"),
        temperature=float(c.get("temperature", 0.85)),
        max_tokens=int(c.get("max_tokens", 2000)),
        timeout=int(c.get("timeout", 60)),
        max_retries=int(c.get("max_retries", 3)),
        rpm=int(c.get("rpm", 20)),
    )
