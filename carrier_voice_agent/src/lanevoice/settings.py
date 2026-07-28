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
    tts_model: str = Field(default="canopylabs/orpheus-v1-english", validation_alias="TTS_MODEL")
    tts_voice: str = Field(default="troy", validation_alias="TTS_VOICE")

    # Bias Whisper toward the words a carrier actually says on these calls.
    stt_prompt: str = Field(
        default=(
            "Freight carrier sales call. Load IDs like L1001, L1002, L1003. "
            "MC and USDOT numbers. Rates in dollars such as $2000, $2300. "
            "Dry van, reefer, flatbed; pickup dates; driver and truck numbers."
        ),
        validation_alias="STT_PROMPT",
    )

    # --- Agent behavior ----------------------------------------------------- #
    use_llm: bool = Field(default=False, validation_alias="USE_LLM")
    allow_interruptions: bool = Field(default=True, validation_alias="ALLOW_INTERRUPTIONS")
    # Wait this long after the caller stops before replying. Higher = fewer
    # cut-offs / fragment replies on a noisy phone line.
    min_endpointing_delay: float = Field(default=1.3, validation_alias="MIN_ENDPOINTING_DELAY")
    max_endpointing_delay: float = Field(default=8.0, validation_alias="MAX_ENDPOINTING_DELAY")

    # --- Negotiation policy ------------------------------------------------- #
    # open_rate = Load Board Rate (floor); ceiling_rate = Max Buy (hard cap).
    max_negotiation_rounds: int = Field(default=8, validation_alias="MAX_NEGOTIATION_ROUNDS")
    # Reserve held BELOW the Max Buy. 0 = the agent may go all the way to Max Buy
    # (never above). Set > 0 to keep some room back for a human.
    negotiation_buffer: float = Field(default=0.0, validation_alias="NEGOTIATION_BUFFER")
    # Share of the REMAINING GAP the agent covers with its one closing move. 0.5
    # meets the carrier in the middle; lower closes firmer. The agent never walks
    # its own number up step by step — it holds, asks the carrier to come closer
    # (see NEGOTIATION_MAX_PULLS), then makes a single decisive offer.
    negotiation_reciprocity: float = Field(
        default=0.5, validation_alias="NEGOTIATION_RECIPROCITY"
    )
    # How far up the floor->Max Buy span the agent may commit ON ITS OWN. Above
    # this (but still within Max Buy) the call goes to a human instead — the bot
    # doesn't spend the top of the range unsupervised. 1.0 = full authority.
    negotiation_discretion_rate: float = Field(
        default=0.6, validation_alias="NEGOTIATION_DISCRETION_RATE"
    )
    # Gap (as a share of the span) not worth haggling over — just book it.
    negotiation_settle_gap_rate: float = Field(
        default=0.10, validation_alias="NEGOTIATION_SETTLE_GAP_RATE"
    )
    # Gap (as a share of the span) close enough to play the split-the-difference
    # close instead of trading nickels.
    negotiation_split_gap_rate: float = Field(
        default=0.30, validation_alias="NEGOTIATION_SPLIT_GAP_RATE"
    )
    # How much of the remaining room a carrier who has NEVER moved gets in the
    # best-and-final. Stonewalling must not pay better than negotiating.
    negotiation_stonewall_final_rate: float = Field(
        default=0.5, validation_alias="NEGOTIATION_STONEWALL_FINAL_RATE"
    )
    # How many times the agent restates its number and asks a carrier who is NOT
    # moving to come down, before it puts its best-and-final on the table.
    negotiation_max_holds: int = Field(default=2, validation_alias="NEGOTIATION_MAX_HOLDS")
    # How many times a carrier who IS moving gets credited and asked to come
    # closer before the agent spends anything. This is the core of the strategy:
    # a concession from the carrier earns another ask, not a counter-offer.
    # Raise it to squeeze harder; 0 makes the agent close on their first move.
    negotiation_max_pulls: int = Field(default=2, validation_alias="NEGOTIATION_MAX_PULLS")

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
