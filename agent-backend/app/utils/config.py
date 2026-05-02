from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    es_url: str = "http://localhost:9200"
    es_index: str = "devops-logs-*"

    # Provider-specific API keys.  Set the key for each provider you use.
    # The generic LLM_API_KEY is used as a fallback if a provider key is absent.
    llm_api_key: str           # required generic fallback
    google_api_key: str = ""   # Google AI (gemini-*, gemma-*)
    anthropic_api_key: str = ""  # Anthropic (claude-*)

    llm_model: str = "gemma-4-31b-it"
    # Comma-separated fallback models tried in order when the primary is
    # rate-limited or quota-exhausted.  Example:
    #   LLM_MODEL_FALLBACK=gemini-2.0-flash,claude-haiku-4-5-20251001
    llm_model_fallback: str = ""

    environment: str = "dev"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
