import asyncio
import json
import re

from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from app.log_processor.summarizer import LogSummary
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("llm_client")

_MAX_TOKENS = 1024


async def analyze(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: list,
    summary: LogSummary,
) -> dict:
    user_content = build_user_prompt(service, environment, error_type, severity, key_events, summary)

    if settings.llm_model.startswith(("gemini", "gemma")):
        result = await _call_gemini(user_content)
    else:
        result = await _call_anthropic(user_content)

    result = _guard_llm_result(result, key_events)
    logger.info({"message": "llm_response_received", "service": service, "model": settings.llm_model})
    return result


async def _call_gemini(user_content: str) -> dict:
    import google.generativeai as genai

    genai.configure(api_key=settings.llm_api_key)
    model = genai.GenerativeModel(
        model_name=settings.llm_model,
        system_instruction=SYSTEM_PROMPT,
    )
    response = await asyncio.to_thread(model.generate_content, user_content)
    return _parse_json(response.text)


async def _call_anthropic(user_content: str) -> dict:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.llm_api_key)
    response = await client.messages.create(
        model=settings.llm_model,
        max_tokens=_MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
    return _parse_json(response.content[0].text)


def _parse_json(text: str) -> dict:
    # 1. Try to extract JSON from a markdown code fence (Gemma 4 wraps output in ```json)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return json.loads(m.group(1))
    # 2. Strip standalone fences if the text starts/ends with them
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    # 3. Thinking models prefix JSON with a reasoning trace — scan for the
    #    first { that successfully parses as a complete top-level object.
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
