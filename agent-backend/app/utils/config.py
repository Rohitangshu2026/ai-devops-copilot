from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    es_url: str = "http://localhost:9200"
    es_index: str = "devops-logs-*"

    # Provider-specific API keys — comma-separated, rotated on rate-limit.
    # Set at least one key per provider you use.
    # The generic LLM_API_KEY is the last-resort fallback when no provider key is set.
    # Optional at startup — the LLM client validates at call time whether any key is available.
    llm_api_key: str = ""         # generic fallback; can be empty if provider keys are set
    google_api_keys: str = ""     # Google AI (gemini-*, gemma-*)  e.g. "key1,key2"
    anthropic_api_keys: str = ""  # Anthropic (claude-*)           e.g. "key1,key2"
    openai_api_keys: str = ""     # OpenAI   (gpt-*, o1-*, o3-*, o4-*)

    llm_model: str = "gemma-4-31b-it"
    # Comma-separated fallback models tried in order when the primary is
    # rate-limited or quota-exhausted.  Example:
    #   LLM_MODEL_FALLBACK=gemini-2.0-flash,claude-haiku-4-5-20251001
    llm_model_fallback: str = ""

    environment: str = "dev"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
