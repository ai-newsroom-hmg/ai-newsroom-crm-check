"""Settings — Phase 1a nutzt nur KG-PG. Spätere Phasen erweitern."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # Phase 1a — required for KG lookup
    kg_pg_dsn: str = ""

    # Phase 1b+
    ni_pg_dsn: str = ""

    # Phase 1f — PressRelations via wraite Cloud-SQL (STRIKT READ-ONLY)
    # Topology: lokal Mac/Tailscale -> 127.0.0.1:5434 via cloud-sql-proxy;
    # GKE -> wraite-proxy.early-signals.svc.cluster.local:5432
    # User `gunterclaude` ist Mitglied `n8n_rw` mit technischen Schreibrechten —
    # NIEMALS schreibend nutzen. Node sendet ausschliesslich SELECT.
    wraite_db_host: str = ""
    wraite_db_port: int = 5434
    wraite_db_name: str = "postgres"
    wraite_db_user: str = "gunterclaude"
    wraite_db_password: str = ""

    # Phase 1c
    ollama_url: str = "http://ruediger.local:11434"
    ollama_model: str = "llama3.3:70b"

    # Phase 1d (service mode)
    hugoplus_user: str = ""
    hugoplus_pass: str = ""
    jwt_secret: str = ""


def get_settings() -> Settings:
    return Settings()
