"""
Application settings — single source of truth for configuration.

Values are read from environment variables (and a local `.env`), with typed
defaults. Change a model or a behavior knob here or via env; nothing else in
the codebase hard-codes these values.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Credentials -------------------------------------------------------- #
    livekit_url: str = Field(default="", validation_alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", validation_alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", validation_alias="LIVEKIT_API_SECRET")
    groq_api_key: str = Field(default="", validation_alias="GROQ_API_KEY")

    # --- Groq models (swap to change models) -------------------------------- #
    stt_model: str = Field(default="whisper-large-v3-turbo", validation_alias="STT_MODEL")
    llm_model: str = Field(default="llama-3.1-8b-instant", validation_alias="LLM_MODEL")
    tts_model: str = Field(default="playai-tts", validation_alias="TTS_MODEL")
    tts_voice: str = Field(default="Celeste-PlayAI", validation_alias="TTS_VOICE")

    # --- Agent behavior ----------------------------------------------------- #
    use_llm: bool = Field(default=False, validation_alias="USE_LLM")
    min_endpointing_delay: float = Field(default=0.8, validation_alias="MIN_ENDPOINTING_DELAY")
    max_endpointing_delay: float = Field(default=8.0, validation_alias="MAX_ENDPOINTING_DELAY")

    # --- Negotiation policy ------------------------------------------------- #
    max_negotiation_rounds: int = Field(default=6, validation_alias="MAX_NEGOTIATION_ROUNDS")
    negotiation_buffer: float = Field(default=150.0, validation_alias="NEGOTIATION_BUFFER")

    # --- Storage ------------------------------------------------------------ #
    db_path: str = Field(default="carrier_agent.db", validation_alias="DB_PATH")

    # --- Observability ------------------------------------------------------ #
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    def require(self, *names: str) -> None:
        """Raise if any named credential is unset. Call before starting the worker."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env = ", ".join(n.upper() for n in missing)
            raise RuntimeError(
                f"Missing required settings: {env}. "
                "Copy .env.example to .env and fill them in."
            )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so every module shares one Settings instance."""
    return Settings()
