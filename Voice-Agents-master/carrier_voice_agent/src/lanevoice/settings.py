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
        # Both plausible places to stand: the repo root (where the `.env` usually
        # lives) and this project directory. Listed root-first so a project-local
        # `.env` wins if someone has both. `lanevoice.env.load_env()` is the real
        # mechanism — it searches upward from the working directory — and these
        # are the belt-and-braces fallback for constructing Settings directly.
        env_file=("../.env", ".env"),
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
    #
    # Keep this a bare vocabulary list, NOT sentences. Whisper treats the prompt as
    # preceding context, so on a short or noisy turn it will happily continue it
    # into the transcript — a sentence like "Rates in dollars such as $2000" came
    # back verbatim in front of a caller's MC number on a live call. Terms alone
    # bias the vocabulary without giving it a sentence to finish.
    stt_prompt: str = Field(
        default=(
            "L1001 L1002 L1003 MC USDOT dry van reefer flatbed deadhead "
            "lane rate per mile empty pickup delivery appointment rate con"
        ),
        validation_alias="STT_PROMPT",
    )

    # --- Agent behavior ----------------------------------------------------- #
    # The agent has no scripted replies: every line it speaks is composed by the
    # LLM from the dialogue, the facts and a directive. Turning this off leaves it
    # unable to talk, so it is only useful for offline tests that drive the state
    # machine with a stub composer.
    use_llm: bool = Field(default=True, validation_alias="USE_LLM")
    # Sanctioned wording is retried before we give up on a turn: the engine's
    # numbers are non-negotiable, so a reply that leaks a rate or drops our offer
    # gets re-prompted with the specific breach named.
    llm_attempts: int = Field(default=3, validation_alias="LLM_ATTEMPTS")
    llm_temperature: float = Field(default=0.5, validation_alias="LLM_TEMPERATURE")
    # Enough room for the full load pitch (lane, dates, windows, commodity,
    # miles, rate) in one turn; short turns simply come back short.
    llm_max_tokens: int = Field(default=220, validation_alias="LLM_MAX_TOKENS")
    llm_read_max_tokens: int = Field(default=120, validation_alias="LLM_READ_MAX_TOKENS")
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

    # --- Where loads and carriers come from --------------------------------- #
    # "transportpro" = the live system of record (needs TRANSPORT_PRO_* below).
    # "sqlite"       = the offline seed data, for the demo and the test suite.
    data_source: str = Field(default="transportpro", validation_alias="DATA_SOURCE")

    transport_pro_url: str = Field(default="", validation_alias="TRANSPORT_PRO_URL")
    transport_pro_username: str = Field(
        default="", validation_alias="TRANSPORT_PRO_USERNAME")
    transport_pro_password: str = Field(
        default="", validation_alias="TRANSPORT_PRO_PASSWORD")
    # A carrier is on the line waiting for an answer, so this is short on purpose.
    transport_pro_timeout: float = Field(
        default=10.0, validation_alias="TRANSPORT_PRO_TIMEOUT")

    # The ONLY load statuses the agent will sell. A load has to be in one of
    # these AND have posting turned on before it is offered to a carrier.
    #
    # "Ready To Dispatch" is the desk's requirement. It is configurable because
    # the status vocabulary differs across Transport Pro endpoints — the API
    # collection's `/voiceai/load/search_available` example answers with
    # "AVAILABLE" while `/load/search` filters on "ready to dispatch" — and being
    # wrong here in the strict direction means the agent can sell nothing at all.
    # A load rejected on status is logged with the value that was actually seen,
    # and `lanevoice-tpcheck` reports it, so a vocabulary mismatch is a one-line
    # env change rather than a code change. Comma-separated; case and
    # punctuation are normalised.
    transport_pro_open_load_statuses: str = Field(
        default="ready to dispatch",
        validation_alias="TRANSPORT_PRO_OPEN_LOAD_STATUSES",
    )

    @property
    def open_load_statuses(self) -> frozenset[str]:
        """The configured statuses, normalised for comparison.

        Deliberately normalised by the same function the mapper compares with:
        two copies of "lowercase it and fold the punctuation" would eventually
        disagree, and the failure would be a load silently not being sold.
        Imported inside the property so the offline path never pulls in the
        integration package.
        """
        from lanevoice.integrations.transportpro.mappers import normalize_status

        return frozenset(
            normalized
            for part in self.transport_pro_open_load_statuses.split(",")
            if (normalized := normalize_status(part))
        )

    # Transport Pro publishes a Load Board Rate and a Max Buy but no fraud
    # tripwire, so the "suspiciously cheap" threshold is derived from the board
    # rate. An ask below this share of it goes to review instead of being booked:
    # a carrier bidding far under the market is the classic double-brokering tell.
    transport_pro_fraud_low_ratio: float = Field(
        default=0.5, validation_alias="TRANSPORT_PRO_FRAUD_LOW_RATIO")
    # How many load numbers the agent will read out when a caller's number misses.
    transport_pro_max_offered_loads: int = Field(
        default=5, validation_alias="TRANSPORT_PRO_MAX_OFFERED_LOADS")
    # How far ahead `/load/search` looks for those alternatives. It wants a pickup
    # window, and a carrier calling today is looking for freight this week.
    transport_pro_open_load_days: int = Field(
        default=7, validation_alias="TRANSPORT_PRO_OPEN_LOAD_DAYS")
    # One repository is shared by every concurrent call, so reads are cached
    # briefly rather than per-call. Loads expire quickly — one can be covered by
    # somebody else mid-call — while a carrier's vetting status does not move.
    transport_pro_load_cache_seconds: float = Field(
        default=60.0, validation_alias="TRANSPORT_PRO_LOAD_CACHE_SECONDS")
    transport_pro_carrier_cache_seconds: float = Field(
        default=300.0, validation_alias="TRANSPORT_PRO_CARRIER_CACHE_SECONDS")
    # The desk gate the agent enforces is ACTIVE AUTHORITY. `carrier_status` has
    # no documented response shape, so by default a record that says nothing
    # about insurance is not treated as uninsured — otherwise a missing field
    # would decline every carrier who called. Turn this on once the live payload
    # is known to carry insurance, and a missing one becomes a hard stop.
    transport_pro_require_insurance_field: bool = Field(
        default=False, validation_alias="TRANSPORT_PRO_REQUIRE_INSURANCE_FIELD")
    # Mirror the agent's call notes onto the load in the system of record, so the
    # rep who picks the load up sees what happened on the call. Best-effort and
    # never able to affect what the caller hears. No-op when DATA_SOURCE=sqlite,
    # where the notes are already local.
    post_load_notes: bool = Field(
        default=True, validation_alias="POST_LOAD_NOTES")

    @property
    def uses_transport_pro(self) -> bool:
        return self.data_source.strip().lower() in ("transportpro", "transport_pro", "tms")

    @property
    def numeric_load_ids(self) -> bool:
        """True when load numbers are bare digits (1303369) rather than L1001.

        Transport Pro load ids are numeric, and the seed data is L-prefixed, so
        what the agent should listen for depends on where loads come from.
        """
        return self.uses_transport_pro

    # --- Storage ------------------------------------------------------------ #
    # Still used when DATA_SOURCE=transportpro: Transport Pro has no endpoint for
    # a call audit trail, so calls, offers, notes and handoffs are recorded here.
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
