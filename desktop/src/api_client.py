#!/usr/bin/env python3
"""大模型 API 客户端：OpenAI 兼容 / Anthropic Claude / Google Gemini。

只用标准库 urllib，不引入第三方依赖。
- 三套协议各自组装请求、解析返回，统一成 Reply(text, 用量, 结束原因)。
- 网络错误、限流、5xx 自动退避重试；401/403/404 等直接给出可操作的提示。
- 部分服务不支持 response_format / temperature，遇到 400 会自动降级重试一次。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

import api_config
import api_ssl

USER_AGENT = "K12QuestionBank/1.10.1 (+https://github.com/mtvtopcom/k12-question-bank)"

_SSL_STATE: Dict[str, Any] = {"ctx": None, "note": ""}


def ssl_context() -> Optional[ssl.SSLContext]:
    """本机根证书上下文；拿不到可用证书时返回 None（走 Python 默认）。"""
    if _SSL_STATE["ctx"] is None:
        try:
            ctx, note = api_ssl.prepare(os.path.join(api_config.data_dir(), "cacert.pem"))
        except Exception as exc:  # noqa: BLE001 —— 兜底失败不能拖垮出题
            return None if not _SSL_STATE["ctx"] else _SSL_STATE["ctx"]
        _SSL_STATE["ctx"] = ctx
        _SSL_STATE["note"] = note
    return _SSL_STATE["ctx"]


def ssl_note() -> str:
    """证书库来源说明，界面在首次请求后可以提示一句。"""
    ssl_context()
    return str(_SSL_STATE.get("note") or "")


def _is_cert_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    text = str(exc)
    return ("CERTIFICATE_VERIFY_FAILED" in text
            or "certificate verify failed" in text.lower())


class LLMError(Exception):
    """带分类的 API 错误，界面据此给出中文提示。"""

    def __init__(self, message: str, kind: str = "api", status: Optional[int] = None,
                 retryable: bool = False, hint: str = "", body: str = "",
                 retry_after: float = 0.0):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable
        self.hint = hint
        self.body = body
        self.retry_after = retry_after

    def friendly(self) -> str:
        text = str(self)
        return f"{text}（{self.hint}）" if self.hint else text


class Reply:
    __slots__ = ("text", "prompt_tokens", "completion_tokens", "model", "finish_reason")

    def __init__(self, text: str, prompt_tokens: int = 0, completion_tokens: int = 0,
                 model: str = "", finish_reason: str = ""):
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = model
        self.finish_reason = finish_reason


# ---------------- URL 归一化 ----------------
def normalize_openai_url(base: str) -> str:
    """把用户填的 Base URL 补成完整的 chat/completions 地址。"""
    url = (base or "").strip()
    if not url:
        raise LLMError("Base URL 不能为空", kind="config",
                       hint="例：https://api.deepseek.com/v1")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if re.search(r"/v\d+[a-zA-Z]*$", url):
        return url + "/chat/completions"
    path = urllib.parse.urlsplit(url).path
    if path in ("", "/"):
        return url + "/v1/chat/completions"
    return url + "/chat/completions"


def normalize_anthropic_url(base: str) -> str:
    url = (base or "").strip()
    if not url:
        raise LLMError("Base URL 不能为空", kind="config",
                       hint="默认 https://api.anthropic.com")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.rstrip("/")
    if url.endswith("/messages"):
        return url
    if re.search(r"/v\d+$", url):
        return url + "/messages"
    if urllib.parse.urlsplit(url).path in ("", "/"):
        return url + "/v1/messages"
    return url + "/messages"


def normalize_gemini_url(base: str, model: str) -> str:
    url = (base or "").strip()
    if not url:
        raise LLMError("Base URL 不能为空", kind="config",
                       hint="默认 https://generativelanguage.googleapis.com/v1beta")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = url.rstrip("/")
    if ":generateContent" in url:
        return url
    if not re.search(r"/v\d+[a-zA-Z]*$", url):
        url += "/v1beta"
    name = (model or "").strip()
    if name.startswith("models/"):
        name = name[len("models/"):]
    if not name:
        raise LLMError("模型名不能为空", kind="config", hint="例：gemini-2.5-flash")
    return f"{url}/models/{urllib.parse.quote(name, safe='')}:generateContent"


# ---------------- HTTP ----------------
def _error_from_http(status: int, body: str, headers: Any) -> LLMError:
    message = ""
    try:
        data = json.loads(body)
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("type") or ""
        elif isinstance(err, str):
            message = err
        elif isinstance(data.get("message"), str):
            message = data["message"]
    except Exception:  # noqa: BLE001
        message = body.strip()
    message = re.sub(r"\s+", " ", message)[:300] or "无返回内容"

    hints = {
        400: "请求参数被拒绝，请检查模型名、Base URL 与该服务是否支持所选参数",
        401: "API Key 无效或已过期，请重新填写",
        403: "没有访问权限：可能未开通该模型、Key 受限或账户欠费",
        404: "接口地址或模型不存在，请检查 Base URL 与模型名",
        429: "触发限流或额度不足，请降低频率或稍后再试",
        500: "服务端错误，稍后重试", 502: "网关错误，稍后重试",
        503: "服务暂不可用，稍后重试", 504: "网关超时，稍后重试",
    }
    if status == 401:
        kind, retryable = "auth", False
    elif status == 403:
        kind, retryable = "auth", False
    elif status == 404:
        kind, retryable = "not_found", False
    elif status == 429:
        kind, retryable = "rate", True
    elif status >= 500:
        kind, retryable = "server", True
    elif status == 400:
        kind, retryable = "bad_request", False
    else:
        kind, retryable = "api", False
    return LLMError(f"HTTP {status}: {message}", kind=kind, status=status,
                    retryable=retryable, hint=hints.get(status, ""), body=body,
                    retry_after=_retry_after(headers))


def _retry_after(headers: Any) -> float:
    raw = ""
    try:
        raw = headers.get("Retry-After") or ""
    except Exception:  # noqa: BLE001
        return 0.0
    if not raw:
        return 0.0
    try:
        return max(0.0, min(60.0, float(raw)))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(raw)
        return max(0.0, min(60.0, when.timestamp() - time.time()))
    except Exception:  # noqa: BLE001
        return 0.0


def _post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any],
               timeout: float) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl_context()) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise _error_from_http(exc.code, body, exc.headers) from None
    except (TimeoutError, socket.timeout):
        raise LLMError(f"请求超时（{int(timeout)} 秒）", kind="network", retryable=True,
                       hint="把「超时(秒)」调到 180–300，或减少每次请求题数 / 调小输出上限") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if _is_cert_error(reason):
            raise LLMError(str(reason), kind="ssl", retryable=False,
                           hint=api_ssl.hint(str(reason))) from None
        raise LLMError(f"网络连接失败：{reason}", kind="network", retryable=True,
                       hint="检查网络 / 代理，以及 Base URL 是否可访问") from None
    except (ssl.SSLError, ssl.CertificateError) as exc:
        if _is_cert_error(exc):
            raise LLMError(str(exc), kind="ssl", retryable=False,
                           hint=api_ssl.hint(str(exc))) from None
        raise LLMError(f"TLS 握手失败：{exc}", kind="network", retryable=True,
                       hint="确认 Base URL 是 https 且未被代理劫持") from None
    except OSError as exc:
        if _is_cert_error(exc):
            raise LLMError(str(exc), kind="ssl", retryable=False,
                           hint=api_ssl.hint(str(exc))) from None
        raise LLMError(f"网络异常：{exc}", kind="network", retryable=True) from None
    if not raw:
        raise LLMError("服务返回空响应", kind="parse", retryable=True)
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        snippet = raw.decode("utf-8", "ignore")[:200]
        raise LLMError(f"返回内容不是 JSON：{exc}；{snippet}", kind="parse",
                       retryable=True) from None


class _BaseClient:
    provider = ""

    def __init__(self, profile: Dict[str, Any]):
        self.profile = dict(profile or {})
        self.base_url = str(self.profile.get("base_url") or "").strip()
        self.model = str(self.profile.get("model") or "").strip()
        self.api_key = str(self.profile.get("api_key") or "").strip()
        self.temperature = _as_float(self.profile.get("temperature"), 0.8, 0.0, 2.0)
        self.max_tokens = _as_int(self.profile.get("max_tokens"), 8192, 64, 1000000)
        self.timeout = _as_float(self.profile.get("timeout"), 90, 10, 900)
        self.interval = _as_float(self.profile.get("interval"), 0.8, 0.0, 120.0)
        self.json_mode = bool(self.profile.get("json_mode", True))
        self.supports_json_mode = self.json_mode
        self.max_retries = _as_int(self.profile.get("retries"), 3, 1, 6)
        # 关思考：各家字段名不同，0=thinking 1=enable_thinking 2=已放弃（服务不支持）
        self.no_thinking = bool(self.profile.get("no_thinking", True))
        self._thinking_step = 0
        self._last_call = 0.0
        # 由调用方注入（Runner 接到界面日志）。重试必须可见，
        # 否则一次 90 秒超时 + 静默重试会让界面看起来完全卡死
        self.log: Optional[Callable[[str], None]] = None
        if not self.api_key:
            raise LLMError("还没有填写 API Key", kind="config", hint="在上方「API Key」输入框粘贴")

    _KIND_LABELS = {"rate": "触发限流", "server": "服务端错误", "network": "网络异常",
                    "parse": "返回内容无法解析", "api": "接口返回错误"}

    def _retry_text(self, exc: "LLMError") -> str:
        """重试日志里用中文说明原因，别把裸的 HTTP 文本甩给用户。"""
        text = str(exc)
        label = self._KIND_LABELS.get(exc.kind, "")
        if label and not text.startswith(("请求", "网络", "返回")):
            return f"{label}（{text}）"
        return text

    def _note(self, text: str) -> None:
        if self.log is None:
            return
        try:
            self.log(text)
        except Exception:  # noqa: BLE001 —— 日志失败不影响请求
            pass

    def _throttle(self) -> None:
        if self.interval <= 0:
            return
        wait = self.interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _call(self, payload: Dict[str, Any], headers: Dict[str, str]) -> Reply:
        raise NotImplementedError

    def chat(self, system: str, user: str, json_mode: bool = True,
             max_tokens: Optional[int] = None,
             cancel: Optional[Callable[[], bool]] = None) -> Reply:
        """发一次对话请求；自动重试与参数降级。

        cancel：返回 True 表示调用方已暂停/停止，重试链立刻放弃。
        否则被放弃的旧请求会在后台继续重试，既耗额度又可能触发并发限流。
        """
        payload = self._payload(system, user, json_mode, max_tokens)
        # 400 降级要能连续退让（thinking → enable_thinking → 无），所以用循环而非递归
        for _ in range(4):
            try:
                return self._call(payload, self._headers(), cancel)
            except LLMError as exc:
                if exc.status != 400:
                    raise
                body = (exc.body or "").lower()
                changed = False
                # 关思考是各家的非标准字段，最可能触发 400。
                # 只有当错误明确指向别的字段时才跳过它，否则优先降级它。
                blamed_elsewhere = any(k in body for k in (
                    "response_format", "temperature", "max_tokens",
                    "max_completion_tokens"))
                if self.no_thinking and self._thinking_step < 2 and not blamed_elsewhere:
                    changed = self._pop_no_thinking(payload) or changed
                if "response_format" in payload and "response_format" in body:
                    payload.pop("response_format", None)
                    self.supports_json_mode = False
                    changed = True
                if "temperature" in payload and "temperature" in body:
                    payload.pop("temperature", None)
                    changed = True
                # 新版 OpenAI 模型改名为 max_completion_tokens
                if (self.provider == "openai" and "max_tokens" in payload
                        and "max_completion_tokens" in body):
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    changed = True
                if not changed:
                    raise
        raise LLMError("多次调整请求参数后仍被拒绝", kind="bad_request", status=400,
                       hint="请检查模型名与该服务支持的参数")

    def _apply_no_thinking(self, payload: Dict[str, Any]) -> None:
        """关思考参数（各协议写法不同，由子类实现）。"""
        return

    def _pop_no_thinking(self, payload: Dict[str, Any]) -> bool:
        """400 时退让一级；返回 True 表示 payload 已改动、可以重试。"""
        return False

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json", "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT}

    def _payload(self, system: str, user: str, json_mode: bool,
                 max_tokens: Optional[int]) -> Dict[str, Any]:
        raise NotImplementedError


class OpenAICompatClient(_BaseClient):
    provider = "openai"

    def _url(self) -> str:
        return normalize_openai_url(self.base_url)

    def _headers(self) -> Dict[str, str]:
        headers = super()._headers()
        headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(self, system: str, user: str, json_mode: bool,
                 max_tokens: Optional[int]) -> Dict[str, Any]:
        if not self.model:
            raise LLMError("模型名不能为空", kind="config", hint="例：deepseek-chat")
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
        }
        if json_mode and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        self._apply_no_thinking(payload)
        return payload

    def _apply_no_thinking(self, payload: Dict[str, Any]) -> None:
        """主流国产服务（豆包 / DeepSeek / GLM / Kimi）都用 thinking.type=disabled。"""
        if not self.no_thinking or self._thinking_step >= 2:
            return
        if self._thinking_step == 0:
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["enable_thinking"] = False        # 通义千问等用这个写法

    def _pop_no_thinking(self, payload: Dict[str, Any]) -> bool:
        if self._thinking_step == 0:
            payload.pop("thinking", None)
            self._thinking_step = 1
            self._apply_no_thinking(payload)      # 换成 enable_thinking 再试
            return True
        if self._thinking_step == 1:
            payload.pop("enable_thinking", None)
            self._thinking_step = 2
            self._note("  该模型不支持关闭深度思考，已改回默认方式（会慢一些）")
            return True
        return False

    def _call(self, payload: Dict[str, Any], headers: Dict[str, str],
              cancel: Optional[Callable[[], bool]] = None) -> Reply:
        url = self._url()
        last: Optional[LLMError] = None
        for attempt in range(self.max_retries):
            if cancel is not None and cancel():
                raise LLMError("已暂停，本次请求已放弃", kind="cancelled")
            self._throttle()
            try:
                data = _post_json(url, headers, payload, self.timeout)
                return _parse_openai(data, self.model)
            except LLMError as exc:
                last = exc
                if not exc.retryable or attempt >= self.max_retries - 1:
                    raise
                wait = _backoff(attempt, exc)
                self._note(f"  {self._retry_text(exc)}；{wait:.0f} 秒后重试"
                           f"（第 {attempt + 2}/{self.max_retries} 次，可点暂停中止）")
                end = time.time() + wait
                while time.time() < end:
                    if cancel is not None and cancel():
                        raise LLMError("已暂停，本次请求已放弃", kind="cancelled")
                    time.sleep(min(0.2, max(0.0, end - time.time())))
        raise last or LLMError("请求失败", kind="api")


class AnthropicClient(_BaseClient):
    provider = "anthropic"

    def _url(self) -> str:
        return normalize_anthropic_url(self.base_url)

    def _headers(self) -> Dict[str, str]:
        headers = super()._headers()
        headers["x-api-key"] = self.api_key
        headers["anthropic-version"] = "2023-06-01"
        return headers

    def _payload(self, system: str, user: str, json_mode: bool,
                 max_tokens: Optional[int]) -> Dict[str, Any]:
        if not self.model:
            raise LLMError("模型名不能为空", kind="config", hint="例：claude-sonnet-4-5")
        # Claude 的 system 是顶层字段，不是 messages 里的一条
        payload = {
            "model": self.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
        }
        self._apply_no_thinking(payload)
        return payload

    def _apply_no_thinking(self, payload: Dict[str, Any]) -> None:
        """Claude 默认就不思考，显式声明一次，避免某些模型默认开思考。"""
        if not self.no_thinking or self._thinking_step >= 2:
            return
        if self._thinking_step == 0:
            payload["thinking"] = {"type": "disabled"}

    def _pop_no_thinking(self, payload: Dict[str, Any]) -> bool:
        if self._thinking_step == 0:
            payload.pop("thinking", None)
            self._thinking_step = 2
            return True
        return False

    def _call(self, payload: Dict[str, Any], headers: Dict[str, str],
              cancel: Optional[Callable[[], bool]] = None) -> Reply:
        url = self._url()
        last: Optional[LLMError] = None
        for attempt in range(self.max_retries):
            if cancel is not None and cancel():
                raise LLMError("已暂停，本次请求已放弃", kind="cancelled")
            self._throttle()
            try:
                data = _post_json(url, headers, payload, self.timeout)
                return _parse_anthropic(data, self.model)
            except LLMError as exc:
                last = exc
                if not exc.retryable or attempt >= self.max_retries - 1:
                    raise
                wait = _backoff(attempt, exc)
                self._note(f"  {self._retry_text(exc)}；{wait:.0f} 秒后重试"
                           f"（第 {attempt + 2}/{self.max_retries} 次，可点暂停中止）")
                end = time.time() + wait
                while time.time() < end:
                    if cancel is not None and cancel():
                        raise LLMError("已暂停，本次请求已放弃", kind="cancelled")
                    time.sleep(min(0.2, max(0.0, end - time.time())))
        raise last or LLMError("请求失败", kind="api")


class GeminiClient(_BaseClient):
    provider = "gemini"

    def _url(self) -> str:
        return normalize_gemini_url(self.base_url, self.model)

    def _headers(self) -> Dict[str, str]:
        headers = super()._headers()
        headers["x-goog-api-key"] = self.api_key
        return headers

    def _payload(self, system: str, user: str, json_mode: bool,
                 max_tokens: Optional[int]) -> Dict[str, Any]:
        gen: Dict[str, Any] = {
            "temperature": self.temperature,
            "maxOutputTokens": int(max_tokens or self.max_tokens),
        }
        if json_mode and self.supports_json_mode:
            gen["responseMimeType"] = "application/json"
        payload: Dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        self._apply_no_thinking(payload)
        return payload

    def _apply_no_thinking(self, payload: Dict[str, Any]) -> None:
        """Gemini 用 thinkingBudget=0 表示不思考；超范围模型会忽略该字段。"""
        if not self.no_thinking or self._thinking_step >= 2:
            return
        if self._thinking_step == 0:
            gen = payload.setdefault("generationConfig", {})
            gen["thinkingConfig"] = {"thinkingBudget": 0}

    def _pop_no_thinking(self, payload: Dict[str, Any]) -> bool:
        if self._thinking_step == 0:
            gen = payload.get("generationConfig") or {}
            gen.pop("thinkingConfig", None)
            self._thinking_step = 2
            self._note("  该模型不支持 thinkingBudget=0，已改回默认方式")
            return True
        return False

    def _call(self, payload: Dict[str, Any], headers: Dict[str, str],
              cancel: Optional[Callable[[], bool]] = None) -> Reply:
        url = self._url()
        last: Optional[LLMError] = None
        for attempt in range(self.max_retries):
            if cancel is not None and cancel():
                raise LLMError("已暂停，本次请求已放弃", kind="cancelled")
            self._throttle()
            try:
                data = _post_json(url, headers, payload, self.timeout)
                return _parse_gemini(data, self.model)
            except LLMError as exc:
                last = exc
                if not exc.retryable or attempt >= self.max_retries - 1:
                    raise
                wait = _backoff(attempt, exc)
                self._note(f"  {self._retry_text(exc)}；{wait:.0f} 秒后重试"
                           f"（第 {attempt + 2}/{self.max_retries} 次，可点暂停中止）")
                end = time.time() + wait
                while time.time() < end:
                    if cancel is not None and cancel():
                        raise LLMError("已暂停，本次请求已放弃", kind="cancelled")
                    time.sleep(min(0.2, max(0.0, end - time.time())))
        raise last or LLMError("请求失败", kind="api")


# ---------------- 返回解析 ----------------
def _parse_openai(data: Dict[str, Any], model: str) -> Reply:
    choices = data.get("choices") or []
    if not choices:
        err = data.get("error") or data
        raise LLMError(f"返回里没有 choices：{str(err)[:200]}", kind="parse", retryable=True)
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):      # 少数服务返回分段结构
        content = "".join(part.get("text", "") for part in content
                          if isinstance(part, dict))
    if not content:
        content = message.get("reasoning_content") or ""
    if not str(content).strip():
        raise LLMError("模型返回了空内容", kind="parse", retryable=True)
    usage = data.get("usage") or {}
    return Reply(str(content),
                 int(usage.get("prompt_tokens") or 0),
                 int(usage.get("completion_tokens") or 0),
                 data.get("model") or model,
                 str(choice.get("finish_reason") or ""))


def _parse_anthropic(data: Dict[str, Any], model: str) -> Reply:
    blocks = data.get("content") or []
    text = "".join(b.get("text", "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise LLMError(f"模型返回了空内容：{str(data)[:200]}", kind="parse", retryable=True)
    usage = data.get("usage") or {}
    return Reply(text,
                 int(usage.get("input_tokens") or 0),
                 int(usage.get("output_tokens") or 0),
                 data.get("model") or model,
                 str(data.get("stop_reason") or ""))


def _parse_gemini(data: Dict[str, Any], model: str) -> Reply:
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise LLMError(f"请求被安全策略拦截：{feedback['blockReason']}", kind="api",
                       hint="换个知识点或调整提示词后重试")
    candidates = data.get("candidates") or []
    if not candidates:
        raise LLMError(f"返回里没有 candidates：{str(data)[:200]}", kind="parse",
                       retryable=True)
    cand = candidates[0] or {}
    parts = ((cand.get("content") or {}).get("parts") or [])
    text = "".join(p.get("text", "") for p in parts
                   if isinstance(p, dict) and not p.get("thought"))
    if not text.strip():
        raise LLMError(f"模型返回了空内容（{cand.get('finishReason', '')}）",
                       kind="parse", retryable=True)
    usage = data.get("usageMetadata") or {}
    return Reply(text,
                 int(usage.get("promptTokenCount") or 0),
                 int(usage.get("candidatesTokenCount") or 0),
                 data.get("modelVersion") or model,
                 str(cand.get("finishReason") or ""))


# ---------------- 工具 ----------------
def _backoff(attempt: int, exc: LLMError) -> float:
    if exc.retry_after:
        return max(0.5, min(60.0, exc.retry_after))
    if exc.status == 429:
        return min(30.0, 2.0 ** (attempt + 1))
    return min(20.0, 1.5 * (2 ** attempt))


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def build_client(profile: Dict[str, Any]) -> _BaseClient:
    provider = (profile or {}).get("provider") or "openai"
    if provider == "anthropic":
        return AnthropicClient(profile)
    if provider == "gemini":
        return GeminiClient(profile)
    return OpenAICompatClient(profile)


def test_connection(profile: Dict[str, Any]) -> Dict[str, Any]:
    """发一条最小请求验证配置；返回 {ok, message, ms, model, reply}。"""
    started = time.time()
    try:
        client = build_client(profile)
        reply = client.chat(
            "你是一个连通性测试助手，只需按要求回复。",
            "请只回复两个字：可用", json_mode=False, max_tokens=32)
        ms = int((time.time() - started) * 1000)
        text = re.sub(r"[`\s]+", " ", reply.text or "").strip()[:24]
        return {"ok": True, "ms": ms, "model": reply.model or client.model,
                "reply": text, "ssl": ssl_note(),
                "message": f"连接成功 · {ms} ms · {reply.model or client.model}"
                           + (f" · 返回「{text}」" if text else "")}
    except LLMError as exc:
        short = f"连接失败：{exc}" if exc.kind == "ssl" else f"连接失败：{exc.friendly()}"
        return {"ok": False, "ms": int((time.time() - started) * 1000),
                "kind": exc.kind, "hint": exc.hint, "message": short}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "ms": int((time.time() - started) * 1000),
                "kind": "unknown", "hint": "",
                "message": f"连接失败：{exc}"}
