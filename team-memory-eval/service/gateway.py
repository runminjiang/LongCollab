"""宿主按实验配置转发模型请求。成员仅持有当前回合的凭证。"""

import json
import os
from threading import Lock
from urllib.parse import urlsplit

import httpx

from service import budget
from service.events import emit

VLLM_BASE_URL = "http://127.0.0.1:8505/v1"


def api_base(value):
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError("base_url 只接受 HTTP(S) 地址和 API 路径，请单独配置密钥")
    return value.rstrip("/")


def is_validation_endpoint(base):
    url = urlsplit(base)
    return url.hostname in {"127.0.0.1", "localhost", "::1"} and url.port == 8505


def endpoint_url(provider, base_url=None, *, judge=False, chat=False):
    """显式地址是 API 前缀，旧供应商环境变量沿用各自的 URL 约定。"""
    if base_url:
        base = api_base(base_url)
    else:
        if provider == "vllm":
            default = VLLM_BASE_URL
        elif provider == "openai":
            default = (
                (os.getenv("TME_JUDGE_BASE_URL") if judge else None)
                or os.getenv("OPENAI_BASE_URL")
                or "https://api.openai.com/v1"
            )
        elif provider == "anthropic":
            default = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        elif chat:
            default = os.getenv("DEEPSEEK_CHAT_BASE_URL", "https://api.deepseek.com")
        else:
            default = os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"
            )
        base = api_base(default)
        if not base.endswith("/v1") and provider in {"anthropic", "deepseek"}:
            return base + "/v1"
    return base if urlsplit(base).path else base + "/v1"


def endpoint(provider, base_url, key_env, *, api_key=None, judge=False, chat=False):
    base = endpoint_url(provider, base_url, judge=judge, chat=chat)
    name = key_env
    if not base_url and not name and api_key is None:
        if provider == "openai":
            name = (
                "TME_JUDGE_API_KEY"
                if judge and os.getenv("TME_JUDGE_API_KEY")
                else "OPENAI_API_KEY"
            )
        elif provider == "anthropic":
            name = (
                "ANTHROPIC_AUTH_TOKEN"
                if os.getenv("ANTHROPIC_AUTH_TOKEN")
                else "ANTHROPIC_API_KEY"
            )
        elif provider == "deepseek":
            name = "DEEPSEEK_API_KEY"
    key = None
    if name:
        key = os.environ.get(name)
        if not key:
            raise ValueError(f"宿主未设置 {name}")
    elif api_key is not None:
        key = api_key.get_secret_value()
    auth = {}
    if key is not None:
        if not key or not key.isascii() or not key.isprintable() or key != key.strip():
            raise ValueError("API key 需填写单行非空 ASCII 文本，首尾不能有空格")
        if (
            provider == "anthropic"
            and name != "ANTHROPIC_AUTH_TOKEN"
            or provider == "deepseek"
            and not chat
        ):
            auth["x-api-key"] = key
        else:
            auth["authorization"] = "Bearer " + key
    return base, auth


def validate_responses_request(body, model):
    if body.get("model") != model:
        raise ValueError("只能调用本次实验指定的模型")
    if any(body.get(key) for key in ("conversation", "previous_response_id", "prompt")):
        raise ValueError("模型请求需要携带本次会话上下文")
    tools = body.get("tools", [])
    for tool in tools:
        if tool.get("type") not in {"function", "custom", "namespace"}:
            raise ValueError("只能声明在容器中执行的工具")
        if tool["type"] == "namespace":
            if any(t.get("type") not in {"function", "custom"} for t in tool["tools"]):
                raise ValueError("不允许声明服务端工具")
    inputs = body.get("input")
    for item in inputs if isinstance(inputs, list) else ():
        if item.get("type") == "item_reference":
            raise ValueError("不允许引用其他模型请求的上下文")
        for content in (item.get("content"), item.get("output")):
            if isinstance(content, list) and any(
                block.get("type") in {"input_image", "input_file", "input_audio"}
                for block in content if isinstance(block, dict)
            ):
                raise ValueError("模型输入仅支持文本")


def validate_marble_messages(body):
    if body.get("mcp_servers") or any(
        t.get("type", "custom") != "custom"
        and not t["type"].startswith(("bash_", "text_editor_"))
        for t in body.get("tools", [])
    ):
        raise ValueError("MARBLE 成员只可使用本地工具和场景工具")

    def has_remote_source(value):
        if isinstance(value, dict):
            return value.get("type") == "url" or any(
                has_remote_source(v) for v in value.values()
            )
        return isinstance(value, list) and any(has_remote_source(v) for v in value)

    if has_remote_source(body.get("messages", [])):
        raise ValueError("MARBLE 模型输入不能引用远程文件")


def messages_body(data):
    """Messages 协议把系统内容放在顶层；保留 SDK 的内容块和工具定义。"""
    if not any(m["role"] == "system" for m in data["messages"]):
        return data

    def blocks(value):
        return (
            [{"type": "text", "text": value}] if isinstance(value, str) else list(value)
        )

    system = blocks(data.get("system", []))
    messages = []
    for message in data["messages"]:
        if message["role"] == "system":
            system.extend(blocks(message["content"]))
        else:
            messages.append(message)
    return {**data, "system": system, "messages": messages}


def member_wire_effort(config, data, *, responses):
    """返回上游生成请求中的档位，并核对原生 SDK 是否应用了实验设置。"""
    requested = getattr(config, "member_reasoning_effort", None)
    if config.provider == "openai" and not responses:
        # Messages 转 Chat Completions。未配置时保留旧实验的端点默认行为。
        return "none" if requested == "off" else requested
    if responses:
        effort = (data.get("reasoning") or {}).get("effort")
    else:
        effort = (data.get("output_config") or {}).get("effort")
        if effort is None and (data.get("thinking") or {}).get("type") == "disabled":
            effort = "off"
    if requested is not None and effort != requested:
        raise ValueError(f"{config.harness} 成员请求的推理档位为 {effort!r}，实验要求 {requested!r}")
    return effort


def openai_messages(data, post_chat, *, reasoning_effort=None):
    """让 Messages 客户端使用 Chat Completions 的原生 tools 和 tool_calls。"""
    # 协议转换使用随宿主安装的固定版本，仅使用其本地转换函数。
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from litellm.llms.anthropic.pass_through.adapters.transformation import (
        AnthropicAdapter,
    )
    from litellm.types.utils import ModelResponse, ModelResponseStream

    adapter = AnthropicAdapter()
    # SDK 可能附带会话日志、插件清单或 Claude 的上下文管理选项。
    # 只转换 Chat Completions 的通用字段，避免将私有扩展发送给第三方。
    fields = (
        "model",
        "messages",
        "system",
        "tools",
        "tool_choice",
        "stream",
        "max_tokens",
        "temperature",
        "top_p",
        "stop_sequences",
        "output_format",
    )
    request = {k: data[k] for k in fields if k in data}
    if output_format := (data.get("output_config") or {}).get("format"):
        request["output_config"] = {"format": output_format}
    chat, names = adapter.translate_completion_input_params_with_tool_mapping(
        request, custom_llm_provider="openai"
    )
    if reasoning_effort is not None:
        chat["reasoning_effort"] = reasoning_effort
    if "disable_parallel_tool_use" in (data.get("tool_choice") or {}):
        chat["parallel_tool_calls"] = not data["tool_choice"][
            "disable_parallel_tool_use"
        ]
    if chat.get("stream"):
        chat["stream_options"] = {"include_usage": True}
    response = post_chat(chat)
    if not response.is_success:
        return response, response.content
    if chat.get("stream"):

        def chunks():
            for line in response.iter_lines():
                if line.startswith("data:"):
                    event = line[5:].strip()
                    if event and event != "[DONE]":
                        value = json.loads(event)
                        if "error" in value:
                            raise ValueError(str(value["error"]))
                        yield ModelResponseStream(**value)

        stream = adapter.translate_completion_output_params_streaming(
            chunks(), data["model"], names, is_async=False
        )
        return httpx.Response(
            200, content=b"".join(stream), headers={"content-type": "text/event-stream"}
        ), response.content
    answer = adapter.translate_completion_output_params(
        ModelResponse(**response.json()), names
    )
    return httpx.Response(200, json=answer), response.content


class Gateway:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.model_lock = Lock()
        self.context_limits = {}

    def _openai_chat(self, client, base, auth, data, actor):
        """将裁剪后的输出上限发给 OpenAI 兼容端点，按明确的参数错误重试。"""
        body = data.copy()
        old_limit = body.pop("max_tokens", None)
        if old_limit is not None:
            current = body.get("max_completion_tokens")
            body["max_completion_tokens"] = (
                min(old_limit, current) if current is not None else old_limit
            )
        if body.get("n") == 1:
            del body["n"]  # 单个回答是接口默认值。
        while True:
            response = client.post(base + "/chat/completions", headers=auth, json=body)
            if response.status_code != 400:
                return response
            try:
                error = response.json()["error"]
            except (ValueError, KeyError, TypeError):
                return response
            if not isinstance(error, dict):
                return response
            param, code = error.get("param"), error.get("code")
            if param not in ("max_completion_tokens", "temperature") or param not in body:
                return response
            if param == "max_completion_tokens" and code in (
                "unsupported_parameter", "unknown_parameter"
            ):
                body["max_tokens"] = body.pop(param)
            elif param == "temperature" and code in (
                "unsupported_parameter", "unsupported_value"
            ):
                del body[param]
            else:
                return response
            # 两个分支各移除一个被拒字段，因此每个字段至多重试一次。
            with self.store.transaction() as db:
                emit(
                    db, "model_parameter_retry", actor=actor, model=body["model"],
                    param=param, code=code,
                )

    def _vllm_limit(self, client, base, headers, data, remaining, *, chat):
        if chat:
            # planner 走 Chat Completions，使用同一服务的聊天模板计数。
            body = {
                k: data[k]
                for k in ("model", "messages", "chat_template_kwargs")
                if k in data
            }
            count = client.post(
                base.removesuffix("/v1") + "/tokenize", headers=headers, json=body
            )
            field = "count"
        else:
            body = {
                k: data[k]
                for k in ("model", "messages", "system", "tools", "tool_choice")
                if k in data
            }
            count = client.post(
                base + "/messages/count_tokens", headers=headers, json=body
            )
            field = "input_tokens"
        if not count.is_success:
            return count
        prompt = count.json()[field]
        if remaining is not None and prompt >= remaining:
            raise budget.Exhausted(
                f"剩余 {remaining} tokens，本次输入需要 {prompt} tokens"
            )
        if base not in self.context_limits:
            models = client.get(base + "/models", headers=headers)
            if not models.is_success:
                return models
            self.context_limits[base] = {
                m["id"]: m.get("max_model_len") for m in models.json()["data"]
            }
        cap = data["max_tokens"]
        if remaining is not None:
            cap = min(cap, remaining - prompt)
        context = self.context_limits[base].get(data["model"])
        if context:
            if prompt >= context:
                return httpx.Response(
                    400,
                    json={
                        "error": f"本次输入为 {prompt} tokens，模型上下文为 {context}"
                    },
                )
            cap = min(cap, context - prompt)
        data["max_tokens"] = cap
        return None

    def forward(self, actor, category, path, payload, headers=None):
        with self.model_lock:
            try:
                return self._forward(actor, category, path, payload, headers)
            except budget.Exhausted as error:
                with self.store.transaction() as db:
                    emit(db, "budget_exhausted", actor=actor, error=str(error))
                raise

    def _forward(self, actor, category, path, payload, headers):
        cfg = self.config
        provider = cfg.provider
        planner = category == "planner"
        native_chat = planner and provider != "anthropic"
        responses = not planner and getattr(cfg, "harness", None) == "codex"
        messages = not native_chat and not responses
        if responses:
            routes = ("/v1/responses",)
        elif native_chat:
            routes = ("/v1/chat/completions",)
        else:
            routes = ("/v1/messages", "/v1/messages/count_tokens")
        if path not in routes:
            raise PermissionError("模型请求路径与配置的协议不符")
        agent_limit = (
            getattr(cfg, "max_agent_tokens", None) if category == "worker" else None
        )
        with self.store.transaction() as db:
            remaining = (
                cfg.max_team_tokens - budget.total(db)
                if cfg.max_team_tokens is not None
                else None
            )
            agent_remaining = (
                agent_limit - budget.agent_used(db, actor)
                if agent_limit is not None
                else None
            )
        if remaining is not None and remaining <= 0:
            raise budget.Exhausted("团队 token 预算已用尽")
        if agent_remaining is not None and agent_remaining <= 0:
            raise budget.Exhausted(f"成员 {actor} 的 {agent_limit} tokens 已用尽")
        base, auth = endpoint(
            provider,
            cfg.base_url,
            cfg.api_key_env,
            api_key=getattr(cfg, "api_key", None),
            chat=native_chat,
        )
        model = (cfg.planner_model or cfg.model) if planner else cfg.model
        data = {**payload, "model": model}
        if responses:
            validate_responses_request(data, model)
            data["store"] = False
        elif not planner and getattr(cfg, "benchmark", None) == "marble":
            validate_marble_messages(data)
        count = path.endswith("/count_tokens")
        if not count:
            cap_field = "max_output_tokens" if responses else "max_tokens"
            cap = cfg.max_output_tokens
            if (
                cap is None
                and provider == "vllm"
                and not planner
                and not responses
                and getattr(cfg, "max_agent_tokens", None) is None
            ):
                cap = 4096  # 兼容原有 vLLM 团队预算实验。
            limits = [
                n
                for n in (data.get(cap_field), cap, remaining, agent_remaining)
                if n is not None
            ]
            if limits and (not responses or cap or cap_field in data):
                data[cap_field] = min(limits)
        if messages:
            data = messages_body(data)
        with httpx.Client(
            trust_env=provider != "vllm"
            and os.getenv("TME_MODEL_TRUST_ENV", "1") != "0",
            timeout=180,
        ) as client:
            response = None
            upstream_usage = None
            wire_effort = None
            if provider == "vllm" and not count and not responses:
                response = self._vllm_limit(
                    client, base, auth, data, remaining, chat=native_chat
                )
            if response is None:
                if not planner and not count:
                    wire_effort = member_wire_effort(cfg, data, responses=responses)
                if provider == "openai" and messages:
                    if count:
                        response = httpx.Response(
                            501,
                            json={"error": "Chat Completions 未定义 count_tokens 接口"},
                        )
                    else:
                        response, upstream_usage = openai_messages(
                            data,
                            lambda chat: self._openai_chat(client, base, auth, chat, actor),
                            reasoning_effort=wire_effort,
                        )
                elif provider == "openai" and native_chat:
                    response = self._openai_chat(client, base, auth, data, actor)
                else:
                    if messages:
                        auth["anthropic-version"] = "2023-06-01"
                        if headers and headers.get("anthropic-beta"):
                            auth["anthropic-beta"] = headers["anthropic-beta"]
                    response = client.post(
                        base + path.removeprefix("/v1"), headers=auth, json=data
                    )
        content = response.content
        usage = (
            token_counts(upstream_usage if upstream_usage is not None else content)
            if response.is_success and not count
            else None
        )
        if (
            (agent_limit is not None or responses)
            and response.is_success
            and not count
            and usage is None
        ):
            raise ValueError("端点未报告 token 用量，无法执行 token 预算")
        tokens, cached = usage or (0, 0)
        if not response.is_success:
            # 某些服务会在鉴权错误正文中回显凭据，这份正文还会送回成员并进入轨迹。
            credential = auth.get("x-api-key") or auth.get(
                "authorization", ""
            ).removeprefix("Bearer ")
            if credential:
                content = content.replace(credential.encode(), b"[REDACTED]")
        with self.store.transaction() as db:
            budget.record(db, actor, category, tokens, cached_tokens=cached)
            emit(
                db,
                "model_request",
                actor=actor,
                category=category,
                model=model,
                provider=provider,
                http_status=response.status_code,
                tokens=tokens,
                cached_tokens=cached,
                **({"member_reasoning_effort": wire_effort} if wire_effort is not None else {}),
            )
            if not response.is_success:
                emit(
                    db,
                    "model_error",
                    actor=actor,
                    path=path,
                    http_status=response.status_code,
                    error=content.decode(errors="replace")[:1200],
                )
        if agent_remaining is not None and tokens - cached > agent_remaining:
            raise budget.Exhausted(
                f"成员 {actor} 本次使用 {tokens - cached} tokens，剩余 {agent_remaining}"
            )
        if responses and remaining is not None and tokens > remaining:
            raise budget.Exhausted(f"团队本次使用 {tokens} tokens，剩余 {remaining}")
        return (
            response.status_code,
            response.headers.get("content-type", "application/json"),
            content,
        )

    def complete(self, prompt):
        chat = self.config.provider != "anthropic"
        path = "/v1/chat/completions" if chat else "/v1/messages"
        data = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8192,
            "temperature": 0,
        }
        if self.config.provider == "vllm":
            data["response_format"] = {"type": "json_object"}
            if self.config.planner_chat_template_kwargs:
                data["chat_template_kwargs"] = self.config.planner_chat_template_kwargs
        status, _, raw = self.forward("planner", "planner", path, data)
        if status != 200:
            raise RuntimeError(
                f"planner HTTP {status}: {raw.decode(errors='replace')[:500]}"
            )
        answer = json.loads(raw)
        if chat:
            choice = answer["choices"][0]
            if not choice["message"].get("content"):
                raise ValueError(
                    f"planner 未返回最终文本，finish_reason={choice.get('finish_reason')}"
                )
            return choice["message"]["content"]
        return "".join(c["text"] for c in answer["content"] if c["type"] == "text")

    def judge_chat(self, incoming, model):
        """评委使用独立配置，推理用量不计入团队预算。"""
        cfg = self.config
        provider = cfg.judge_provider
        base, auth = endpoint(
            provider,
            cfg.judge_base_url,
            cfg.judge_api_key_env,
            api_key=getattr(cfg, "judge_api_key", None),
            judge=True,
            chat=True,
        )
        if provider == "anthropic":
            auth["anthropic-version"] = "2023-06-01"
            path = "/messages"
            data = messages_body(
                {
                    "model": model,
                    "messages": incoming["messages"],
                    "max_tokens": incoming.get("max_tokens")
                    or incoming.get("max_completion_tokens")
                    or 2048,
                    **(
                        {"temperature": incoming["temperature"]}
                        if incoming.get("temperature") is not None
                        else {}
                    ),
                }
            )
        else:
            path = "/chat/completions"
            data = {**incoming, "model": model}
        with httpx.Client(
            trust_env=provider != "vllm"
            and os.getenv("TME_MODEL_TRUST_ENV", "1") != "0",
            timeout=180,
        ) as client:
            if provider == "openai":
                response = self._openai_chat(client, base, auth, data, "judge")
            else:
                response = client.post(base + path, headers=auth, json=data)
        response.raise_for_status()
        answer = response.json()
        tokens, cached = token_counts(response.content) or (0, 0)
        with self.store.transaction() as db:
            budget.record(
                db,
                "judge",
                "judge",
                tokens,
                answer.get("usage", {}).get("cost"),
                cached_tokens=cached,
            )
            emit(db, "judge_request", model=model, provider=provider, tokens=tokens)
        if provider != "anthropic":
            return answer
        text = "".join(c["text"] for c in answer["content"] if c["type"] == "text")
        return {
            "id": answer["id"],
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": text},
                }
            ],
            "usage": {
                "prompt_tokens": answer["usage"].get("input_tokens", 0),
                "completion_tokens": answer["usage"].get("output_tokens", 0),
                "total_tokens": tokens,
            },
        }


def token_counts(raw):
    """返回原始总量与其中的缓存命中量。优先使用端点原始响应，避免协议转换丢失细项。"""
    try:
        frames = [json.loads(raw)]
    except (ValueError, UnicodeError):
        frames = []
        for line in raw.splitlines():
            if line.startswith(b"data:"):
                try:
                    frames.append(json.loads(line[5:]))
                except ValueError:
                    continue
    counts = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        usage = (
            frame.get("usage") or frame.get("message", {}).get("usage")
            or (frame.get("response") or {}).get("usage") or {}
        )
        counts.update({k: v for k, v in usage.items() if isinstance(v, int)})
        details = (
            usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        )
        if isinstance(details.get("cached_tokens"), int):
            counts["cached_input_tokens"] = details["cached_tokens"]
    if not counts.keys() & {
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
    }:
        return None
    if "total_tokens" in counts:
        total = counts["total_tokens"]
    elif "prompt_tokens" in counts or "completion_tokens" in counts:
        total = counts.get("prompt_tokens", 0) + counts.get("completion_tokens", 0)
    else:
        # Messages 将命中缓存、写入缓存与普通输入分列。
        total = sum(
            counts.get(k, 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
    cached = counts.get(
        "cached_input_tokens",
        counts.get("cache_read_input_tokens", counts.get("prompt_cache_hit_tokens", 0)),
    )
    return total, min(total, max(0, cached))


def usage_tokens(raw):
    return (token_counts(raw) or (0, 0))[0]
