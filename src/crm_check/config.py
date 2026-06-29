"""Settings — Phase 1a nutzt nur KG-PG. Spätere Phasen erweitern."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # Phase 1a — required for KG lookup
    kg_pg_dsn: str = ""

    # Phase 1b+
    ceq_api_url: str = ""
    ceq_api_token: str = ""
    ni_pg_dsn: str = ""

    # Phase 1c
    ollama_url: str = "http://ruediger.local:11434"
    ollama_model: str = "llama3.3:70b"

    # Phase 1d (service mode)
    hugoplus_user: str = ""
    hugoplus_pass: str = ""
    jwt_secret: str = ""


def get_settings() -> Settings:
    return Settings()
