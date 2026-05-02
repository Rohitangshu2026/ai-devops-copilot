import os

# Set required env vars before any app module is imported.
# Tests never call the real LLM or Elasticsearch — these are stubs so that
# pydantic-settings can construct the Settings object in a CI environment
# that has no .env file.
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("ES_URL", "http://localhost:9200")
os.environ.setdefault("LLM_MODEL", "gemma-4-31b-it")
