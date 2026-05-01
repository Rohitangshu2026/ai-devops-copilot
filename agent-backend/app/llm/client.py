import asyncio
import json
import re

from app.llm.prompt import SYSTEM_PROMPT, build_user_prompt
from app.utils.config import settings
from app.utils.logger import get_logger

logger = get_logger("llm_client")


async def analyze(
    service: str,
    environment: str,
    error_type: str,
    severity: str,
    key_events: list,
    raw_evidence: list,
) -> dict:
    user_content = build_user_prompt(service, environment, error_type, severity, key_events, raw_evidence)

    if settings.llm_model.startswith("gemini"):
        result = await _call_gemini(user_content)
    else:
        result = await _call_anthropic(user_content)

    logger.info({"message": "llm_response_received", "service": service, "model": settings.llm_model})
    return result


def _parse_json(text: str) -> dict:
    # strip markdown code fences Gemini sometimes wraps around JSON
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


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
        max_tokens=512,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
    return _parse_json(response.content[0].text)
