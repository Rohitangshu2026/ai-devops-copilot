import asyncio
import json
import re

from anthropic import AsyncAnthropic

from app.core.evaluator import validate_response
from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from app.llm.tools import TOOLS, execute_tool, get_gemini_tools
from app.log_processor.summarizer import LogSummary
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("llm_client")

_MAX_TOKENS     = 1024
_MAX_TOOL_ROUNDS = 3
_LLM_TIMEOUT     = 30.0   # seconds per API call


async def analyze(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: list,
    summary: LogSummary,
    lookback_minutes: int = 30,
) -> dict:
    user_content = build_user_prompt(
        service, environment, error_type, severity, key_events, summary
    )

    for attempt in range(2):
        content = user_content if attempt == 0 else build_user_prompt(
            service, environment, error_type, severity, key_events, summary, strict=True
        )
        if settings.llm_model.startswith(("gemini", "gemma")):
            result = await _call_gemini(content, service, lookback_minutes)
        else:
            result = await _call_anthropic(content, service, lookback_minutes)

        result = _guard_llm_result(result, key_events)
        valid, reason = validate_response(result)
        if valid:
            break
        logger.warning({
            "message": "llm_response_invalid",
            "attempt": attempt + 1,
            "reason": reason,
            "service": service,
        })

    if not valid:
        # Both attempts failed — force safe fallback action
        result.setdefault("proposed_action", {})["type"] = "no_action"
        result["proposed_action"].setdefault("target", service)
        result["proposed_action"]["reason"] = f"validator rejected response: {reason}"

    logger.info({"message": "llm_response_received", "service": service, "model": settings.llm_model})
    return result


# ── Anthropic agentic loop ────────────────────────────────────────────────────

async def _call_anthropic(user_content: str, service: str, lookback_minutes: int) -> dict:
    client   = AsyncAnthropic(api_key=settings.llm_api_key)
    messages = [{"role": "user", "content": user_content}]
    response = None

    for _ in range(_MAX_TOOL_ROUNDS):
        response = await asyncio.wait_for(
            client.messages.create(
                model=settings.llm_model,
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
            return _parse_json(text_block.text if text_block else "{}")

        # Execute every tool_use block in this round
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                output = await execute_tool(block.name, block.input, service, lookback_minutes)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     output,
                })
                logger.info({
                    "message":  "tool_executed",
                    "tool":     block.name,
                    "service":  service,
                })

        # Append assistant turn (serialise content blocks to plain dicts)
        assistant_content = []
        for b in response.content:
            if b.type == "text":
                assistant_content.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                assistant_content.append({
                    "type":  "tool_use",
                    "id":    b.id,
                    "name":  b.name,
                    "input": b.input,
                })
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user",      "content": tool_results})

    # Max rounds reached — extract whatever text we have
    if response:
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block:
            return _parse_json(text_block.text)
    return {}


# ── Gemini / Gemma agentic loop ───────────────────────────────────────────────

async def _call_gemini(user_content: str, service: str, lookback_minutes: int) -> dict:
    import google.generativeai as genai

    genai.configure(api_key=settings.llm_api_key)
    model = genai.GenerativeModel(
        model_name=settings.llm_model,
        system_instruction=SYSTEM_PROMPT,
        tools=[get_gemini_tools()],
    )
    chat     = model.start_chat()
    response = await asyncio.to_thread(chat.send_message, user_content)

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
            tool_parts.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name,
                        response={"result": output},
                    )
                )
            )
        response = await asyncio.to_thread(chat.send_message, tool_parts)

    return _parse_json(response.text)


# ── JSON parsing helpers ──────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    # 1. Markdown code fence (Gemma 4 always wraps in ```json)
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
