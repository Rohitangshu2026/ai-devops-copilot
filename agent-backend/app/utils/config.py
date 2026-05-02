from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    es_url: str = "http://localhost:9200"
    es_index: str = "devops-logs-*"
    llm_api_key: str
    llm_model: str = "gemma-4-31b-it"
    # Comma-separated fallback models tried in order when the primary is
    # rate-limited or quota-exhausted.  Example:
    #   LLM_MODEL_FALLBACK=gemma-4-31b-it,claude-haiku-4-5-20251001
    llm_model_fallback: str = ""
    environment: str = "dev"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
