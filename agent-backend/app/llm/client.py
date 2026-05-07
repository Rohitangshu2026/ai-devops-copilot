import asyncio
import json
import re
import time
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

from app.core.evaluator import validate_response
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from app.llm.tools import OPENAI_TOOLS, TOOLS, execute_tool, get_gemini_tools
from app.log_processor.summarizer import LogSummary
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("llm_client")

_MAX_TOKENS      = 1024
_MAX_TOOL_ROUNDS = 3
_LLM_TIMEOUT     = 30.0   # seconds per individual API call


# ── Provider helpers ──────────────────────────────────────────────────────────

def _provider(model_name: str) -> str:
    """Classify a model name as 'google', 'openai', or 'anthropic'."""
    if model_name.startswith(("gemini", "gemma")):
        return "google"
    if model_name.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return "openai"
    return "anthropic"


def _keys_for(model_name: str) -> list[str]:
    """
    Return the ordered list of API keys to try for a model.
    Keys are rotated on rate-limit within a model before falling back to the
    next model in the chain.  The generic LLM_API_KEY is the last resort.
    """
    provider = _provider(model_name)
    if provider == "google":
        raw = settings.google_api_keys
    elif provider == "openai":
        raw = settings.openai_api_keys
    else:
        raw = settings.anthropic_api_keys

    keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else []
    return keys or [settings.llm_api_key]


# ── Model chain ───────────────────────────────────────────────────────────────

def _model_chain() -> list[str]:
    """Primary model first, then any comma-separated fallbacks from settings."""
    chain = [settings.llm_model]
    if settings.llm_model_fallback:
        chain.extend(
            m.strip()
            for m in settings.llm_model_fallback.split(",")
            if m.strip() and m.strip() != settings.llm_model
        )
    return chain


def _is_retriable(exc: Exception) -> bool:
    """True for rate-limit / quota-exhausted errors that warrant trying the next key/model."""
    msg = str(exc).lower()
    if any(k in msg for k in ("rate limit", "quota", "too many requests", "429", "resource exhausted")):
        return True
    # Anthropic typed error
    try:
        from anthropic import RateLimitError
        if isinstance(exc, RateLimitError):
            return True
    except ImportError:
        pass
    # Google typed error
    try:
        import google.api_core.exceptions as gexc
        if isinstance(exc, (gexc.ResourceExhausted, gexc.TooManyRequests)):
            return True
    except ImportError:
        pass
    # OpenAI typed error
    try:
        from openai import RateLimitError as OpenAIRateLimitError
        if isinstance(exc, OpenAIRateLimitError):
            return True
    except ImportError:
        pass
    return False


# ── Public entry point ────────────────────────────────────────────────────────

async def analyze(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: list,
    summary: LogSummary,
    lookback_minutes: int = 30,
) -> dict:
    """
    Try each model in the chain.  For each model, rotate through its API keys
    on retriable errors before moving on to the next model.
    """
    chain = _model_chain()
    last_exc: Exception | None = None

    for model_name in chain:
        keys = _keys_for(model_name)
        for i, api_key in enumerate(keys):
            try:
                result = await _analyze_with_model(
                    model_name, api_key,
                    service, environment, error_type,
                    severity, key_events, summary, lookback_minutes,
                )
                logger.info({
                    "message": "llm_response_received",
                    "service": service,
                    "model":   model_name,
                })
                return result
            except Exception as exc:
                if not _is_retriable(exc):
                    raise
                last_exc = exc
                if i < len(keys) - 1:
                    logger.warning({
                        "message":   "key_rotation",
                        "model":     model_name,
                        "key_index": i,
                        "reason":    str(exc),
                    })
                # else: all keys for this model exhausted — fall through to model fallback

        # All keys for this model are exhausted
        if model_name != chain[-1]:
            next_model = chain[chain.index(model_name) + 1]
            logger.warning({
                "message": "model_fallback",
                "from":    model_name,
                "to":      next_model,
                "reason":  str(last_exc),
            })

    raise last_exc  # type: ignore[misc]


async def _analyze_with_model(
    model_name: str,
    api_key: str,
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: list,
    summary: LogSummary,
    lookback_minutes: int,
) -> dict:
    """Run the two-attempt validate-or-retry loop for a single model + key."""
    result: dict = {}
    valid = False
    reason = ""
    provider = _provider(model_name)

    for attempt in range(2):
        content = build_user_prompt(
            service, environment, error_type, severity, key_events, summary,
            strict=(attempt > 0),
        )
        _call_t0 = time.monotonic()
        _prom_result = "ok"
        try:
            if provider == "google":
                result = await _call_gemini(content, service, lookback_minutes, model_name, api_key)
            elif provider == "openai":
                result = await _call_openai(content, service, lookback_minutes, model_name, api_key)
            else:
                result = await _call_anthropic(content, service, lookback_minutes, model_name, api_key)
        except Exception as _exc:
            _prom_result = "rate_limit" if _is_retriable(_exc) else "error"
            try:
                from app.utils.prom_metrics import llm_call_duration, llm_call_total
                llm_call_total.labels(provider=provider, model=model_name, result=_prom_result).inc()
                llm_call_duration.labels(provider=provider).observe(time.monotonic() - _call_t0)
            except Exception:  # noqa: BLE001
                pass
            raise
        else:
            try:
                from app.utils.prom_metrics import llm_call_duration, llm_call_total
                llm_call_total.labels(provider=provider, model=model_name, result=_prom_result).inc()
                llm_call_duration.labels(provider=provider).observe(time.monotonic() - _call_t0)
            except Exception:  # noqa: BLE001
                pass

        result = _guard_llm_result(result, key_events)
        valid, reason = validate_response(result)
        if valid:
            break
        logger.warning({
            "message": "llm_response_invalid",
            "model":   model_name,
            "attempt": attempt + 1,
            "reason":  reason,
            "service": service,
        })

    if not valid:
        result.setdefault("proposed_action", {})["type"] = "no_action"
        result["proposed_action"].setdefault("target", service)
        result["proposed_action"]["reason"] = f"validator rejected response: {reason}"

    return result


# ── Gemini / Gemma agentic loop ───────────────────────────────────────────────

async def _call_gemini(
    user_content: str,
    service: str,
    lookback_minutes: int,
    model_name: str | None = None,
    api_key: str | None = None,
) -> dict:
    import google.generativeai as genai

    name = model_name or settings.llm_model
    key  = api_key or _keys_for(name)[0]
    genai.configure(api_key=key)
    model    = genai.GenerativeModel(
        model_name=name,
        system_instruction=SYSTEM_PROMPT,
        tools=[get_gemini_tools()],
    )
    chat     = model.start_chat()
    response = await asyncio.to_thread(chat.send_message, user_content)

    tool_calls_log: list[dict] = []

    for _ in range(_MAX_TOOL_ROUNDS):
        fn_calls = [
            part.function_call
            for part in response.parts
            if hasattr(part, "function_call") and part.function_call.name
        ]
        if not fn_calls:
            break

        tool_parts = []
        for fc in fn_calls:
            output = await execute_tool(fc.name, dict(fc.args), service, lookback_minutes)
            logger.info({"message": "tool_executed", "tool": fc.name, "service": service})
            tool_calls_log.append({
                "tool": fc.name,
                "args_summary": str(dict(fc.args))[:100],
                "result_summary": output[:100] if output else "",
                "called_at": datetime.now(timezone.utc).isoformat(),
            })
            tool_parts.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name,
                        response={"result": output},
                    )
                )
            )
        response = await asyncio.to_thread(chat.send_message, tool_parts)

    result = _parse_json(response.text) if response.text else {}
    result["_tool_calls"] = tool_calls_log
    return result


# ── Anthropic agentic loop ────────────────────────────────────────────────────

async def _call_anthropic(
    user_content: str,
    service: str,
    lookback_minutes: int,
    model_name: str | None = None,
    api_key: str | None = None,
) -> dict:
    name     = model_name or settings.llm_model
    key      = api_key or _keys_for(name)[0]
    client   = AsyncAnthropic(api_key=key)
    messages = [{"role": "user", "content": user_content}]
    response = None
    tool_calls_log: list[dict] = []

    for _ in range(_MAX_TOOL_ROUNDS):
        response = await asyncio.wait_for(
            client.messages.create(
                model=name,
                max_tokens=_MAX_TOKENS,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=messages,
                tools=TOOLS,
            ),
            timeout=_LLM_TIMEOUT,
        )

        if response.stop_reason != "tool_use":
            text_block = next((b for b in response.content if b.type == "text"), None)
            result = _parse_json(text_block.text if text_block else "{}")
            result["_tool_calls"] = tool_calls_log
            return result

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                output = await execute_tool(block.name, block.input, service, lookback_minutes)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     output,
                })
                logger.info({"message": "tool_executed", "tool": block.name, "service": service})
                tool_calls_log.append({
                    "tool": block.name,
                    "args_summary": str(block.input)[:100],
                    "result_summary": output[:100] if output else "",
                    "called_at": datetime.now(timezone.utc).isoformat(),
                })

        assistant_content = []
        for b in response.content:
            if b.type == "text":
                assistant_content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use", "id": b.id, "name": b.name, "input": b.input,
                })
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user",      "content": tool_results})

    result = {}
    if response:
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block:
            result = _parse_json(text_block.text)
    result["_tool_calls"] = tool_calls_log
    return result


# ── OpenAI agentic loop ───────────────────────────────────────────────────────

async def _call_openai(
    user_content: str,
    service: str,
    lookback_minutes: int,
    model_name: str | None = None,
    api_key: str | None = None,
) -> dict:
    from openai import AsyncOpenAI

    name   = model_name or settings.llm_model
    key    = api_key or _keys_for(name)[0]
    client = AsyncOpenAI(api_key=key)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]
    response = None
    tool_calls_log: list[dict] = []

    for _ in range(_MAX_TOOL_ROUNDS):
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=name,
                max_tokens=_MAX_TOKENS,
                messages=messages,
                tools=OPENAI_TOOLS,
                tool_choice="auto",
            ),
            timeout=_LLM_TIMEOUT,
        )

        choice = response.choices[0]
        if choice.finish_reason != "tool_calls" or not choice.message.tool_calls:
            result = _parse_json(choice.message.content or "{}")
            result["_tool_calls"] = tool_calls_log
            return result

        # Serialize the assistant turn as a plain dict (avoids SDK object in messages list)
        messages.append({
            "role":       "assistant",
            "content":    choice.message.content,
            "tool_calls": [
                {
                    "id":       tc.id,
                    "type":     "function",
                    "function": {
                        "name":      tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.message.tool_calls
            ],
        })

        for tc in choice.message.tool_calls:
            args = json.loads(tc.function.arguments)
            output = await execute_tool(
                tc.function.name,
                args,
                service,
                lookback_minutes,
            )
            logger.info({"message": "tool_executed", "tool": tc.function.name, "service": service})
            tool_calls_log.append({
                "tool": tc.function.name,
                "args_summary": str(args)[:100],
                "result_summary": output[:100] if output else "",
                "called_at": datetime.now(timezone.utc).isoformat(),
            })
            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      output,
            })

    result = {}
    if response:
        last = response.choices[0].message.content
        if last:
            result = _parse_json(last)
    result["_tool_calls"] = tool_calls_log
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    # 1. Markdown code fence (Gemma 4 wraps output in ```json)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return json.loads(m.group(1))
    # 2. Strip standalone fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    # 3. Thinking-model prefix — scan all { positions for first valid top-level object
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text, i)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return json.loads(text)


def _guard_llm_result(result: dict, key_events: list) -> dict:
    """Ensure root_causes is never empty — fallback to a signal-derived cause."""
    causes = result.get("root_causes") or []
    if not causes or not causes[0].get("cause", "").strip():
        event_hint = key_events[0] if key_events else "unknown error pattern"
        result["root_causes"] = [{"cause": f"error detected: {event_hint}", "confidence": 0.6}]
    return result
