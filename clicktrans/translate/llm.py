"""OpenAI 兼容接口的流式翻译客户端。

用 httpx 同步流式（iter_lines），不引入 asyncio/qasync —— 翻译跑在唯一的
工作线程里，同步代码更简单也更容易推理。流式是「实时」体验的来源：
每收到一个 token 就往上抛，卡片边收边渲染。

取消机制是天然的：上层停止迭代这个生成器时，with 块会关闭 response，
in-flight 请求随之中断。
"""

from __future__ import annotations

import json
import time
from typing import Iterator

import httpx

from ..lang import Lang
from ..utils import log
from .base import NetworkError, NoApiKeyError, TranslateError, TranslateRequest
from .prompts import build_messages

_log = log.get("translate")

_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


class LlmTranslator:
    name = "llm"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: int = 20,
        proxy: str = "",
        ja_style: str = "polite",
        temperature: float = 0.2,
        stream: bool = True,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key or ""
        self.timeout_s = max(5, int(timeout_s))
        self.proxy = proxy or ""
        self.ja_style = ja_style
        self.temperature = float(temperature)
        self.use_stream = bool(stream)
        self._client: httpx.Client | None = None

    # ---------------------------------------------------------------- 客户端

    @property
    def endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        timeout = httpx.Timeout(self.timeout_s, connect=min(10.0, self.timeout_s))
        if self.proxy:
            try:
                self._client = httpx.Client(timeout=timeout, proxy=self.proxy)
            except TypeError:
                # httpx < 0.26 用 proxies=
                self._client = httpx.Client(timeout=timeout, proxies=self.proxy)
        else:
            # 留空则读系统代理（httpx 默认 trust_env=True）
            self._client = httpx.Client(timeout=timeout)
        return self._client

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def warmup(self) -> None:
        """提前做掉 TLS 握手，能省下 100-200 ms 的首字延迟。"""
        if not self.api_key or not self.base_url:
            return
        try:
            client = self._get_client()
            client.get(self.base_url, timeout=5.0)
            _log.info("翻译端点连接预热完成")
        except Exception as exc:
            _log.debug("连接预热失败（不影响使用）: %s", exc)

    # ---------------------------------------------------------------- 翻译

    def translate(self, req: TranslateRequest) -> Iterator[str]:
        if not self.api_key:
            raise NoApiKeyError("未配置翻译 API Key，请在托盘菜单里填写。")
        if not self.base_url:
            raise TranslateError("未配置翻译接口地址。")
        if not req.text.strip():
            return

        messages = build_messages(req, self.ja_style)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "stream": self.use_stream,
        }

        produced = False
        if self.use_stream:
            try:
                for chunk in self._stream(payload):
                    produced = True
                    yield chunk
                return
            except TranslateError:
                raise
            except _NETWORK_ERRORS as exc:
                if produced:
                    raise NetworkError("翻译中断：网络连接丢失") from exc
                raise NetworkError("网络不可达，暂时无法翻译") from exc
            except Exception as exc:
                if produced:
                    # 已经吐了一部分，不能重来（会重复），只能如实报错
                    raise TranslateError(f"翻译中断：{exc}") from exc
                _log.warning("流式请求失败，降级为非流式: %s", exc)

        payload["stream"] = False
        try:
            yield self._blocking(payload)
        except TranslateError:
            raise
        except _NETWORK_ERRORS as exc:
            raise NetworkError("网络不可达，暂时无法翻译") from exc

    # ------------------------------------------------------------ 流式实现

    def _stream(self, payload: dict) -> Iterator[str]:
        client = self._get_client()
        with client.stream(
            "POST", self.endpoint, json=payload, headers=self._headers
        ) as response:
            if response.status_code >= 400:
                raise _http_error(response)
            for line in response.iter_lines():
                piece = _parse_sse_line(line)
                if piece is _DONE:
                    return
                if piece:
                    yield piece

    def _blocking(self, payload: dict) -> str:
        client = self._get_client()
        response = client.post(self.endpoint, json=payload, headers=self._headers)
        if response.status_code >= 400:
            raise _http_error(response)
        try:
            data = response.json()
            return data["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            raise TranslateError(f"无法解析翻译接口返回：{exc}") from exc


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

_DONE = object()


def _parse_sse_line(line: str):
    """解析一行 SSE，返回增量文本 / None / _DONE。"""
    if not line:
        return None
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data:
        return None
    if data == "[DONE]":
        return _DONE
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if content is None:
        # 有些实现在最后一块用 message 而不是 delta
        content = (choices[0].get("message") or {}).get("content")
    return content or None


def _http_error(response: httpx.Response) -> TranslateError:
    status = response.status_code
    detail = ""
    try:
        body = response.json()
        detail = (
            (body.get("error") or {}).get("message")
            or body.get("message")
            or ""
        )[:200]
    except Exception:
        try:
            detail = response.text[:200]
        except Exception:
            detail = ""

    hints = {
        401: "API Key 无效或已过期",
        403: "API Key 无权访问该模型",
        404: "接口地址或模型名不对",
        402: "账户余额不足",
        429: "请求过于频繁或额度已用尽",
    }
    hint = hints.get(status)
    if hint is None:
        hint = "服务端错误" if status >= 500 else "请求被拒绝"
    message = f"{hint}（HTTP {status}）"
    if detail:
        message += f"：{detail}"
    return TranslateError(message)


def create_translator(cfg, api_key: str) -> LlmTranslator:
    """按配置构造翻译器。"""
    return LlmTranslator(
        base_url=cfg.base_url,
        model=cfg.model,
        api_key=api_key,
        timeout_s=cfg.timeout_s,
        proxy=cfg.proxy,
        ja_style=cfg.ja_style,
        temperature=cfg.temperature,
        stream=cfg.stream,
    )


def probe_model(cfg, api_key: str) -> tuple[bool, str]:
    """连通性自检，给设置面板的「测试」按钮用。"""
    started = time.perf_counter()
    translator = create_translator(cfg, api_key)
    try:
        req = TranslateRequest(
            text="hello",
            source_lang=Lang.EN,
            target_lang=Lang.ZH,
        )
        text = "".join(translator.translate(req)).strip()
        elapsed = int((time.perf_counter() - started) * 1000)
        if text:
            return True, f"连通正常，{elapsed} ms，返回：{text[:40]}"
        return False, "接口有响应但返回为空"
    except TranslateError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"连接失败：{exc}"
    finally:
        translator.close()
