from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    es_url: str = "http://localhost:9200"
    es_index: str = "devops-logs-*"
    llm_api_key: str
    llm_model: str = "claude-sonnet-4-6"
    environment: str = "dev"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
