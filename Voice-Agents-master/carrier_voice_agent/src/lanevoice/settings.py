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
    # OpenRouter is required for any live call: speech-to-text and text-to-speech
    # both run on it whichever LLM composes the turns. It is also the default
    # composer gateway, so on a stock deployment this is the only AI key needed.
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    # Every OpenRouter call goes through this root: /chat/completions for the
    # composer, /audio/transcriptions for STT, /audio/speech for TTS.
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        validation_alias="OPENROUTER_BASE_URL",
    )

    # --- Which LLM writes the agent's spoken turns --------------------------- #
    # openrouter — Claude Haiku 4.5 through OpenRouter's OpenAI-shaped gateway.
    #              The default, and the one key that already has to be set.
    # anthropic  — the same model via the official SDK, one hop fewer. Needs
    #              ANTHROPIC_API_KEY *in addition to* the OpenRouter key, because
    #              speech still goes through OpenRouter either way.
    #
    # This setting only moves the composer. STT and TTS are always OpenRouter.
    llm_provider: str = Field(default="openrouter", validation_alias="LLM_PROVIDER")

    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")

    # How long to wait on the LLM before giving up on a turn. A carrier is on the
    # line, so this is short: a slow reply is worse than a handed-over call, and
    # the conversation layer already knows how to hand over.
    llm_timeout: float = Field(default=20.0, validation_alias="LLM_TIMEOUT")

    # The model each provider uses when LLM_MODEL is left unset. Both are Claude
    # Haiku 4.5; only the spelling differs. OpenRouter namespaces by vendor with a
    # dotted version, Anthropic uses a hyphenated id and no date suffix.
    _PROVIDER_MODELS = {
        "openrouter": "anthropic/claude-haiku-4.5",
        "anthropic": "claude-haiku-4-5",
    }

    @property
    def llm_api_key(self) -> str:
        """The key the active provider needs, or "" if it isn't configured.

        Named per-provider rather than a single `LLM_API_KEY` so both can sit in
        one `.env` and switching provider is one line, with no re-pasting of
        secrets and no chance of sending an OpenRouter key to Anthropic.
        """
        return {
            "openrouter": self.openrouter_api_key,
            "anthropic": self.anthropic_api_key,
        }.get(self.llm_provider.strip().lower(), "")

    @property
    def llm_key_name(self) -> str:
        """The env var the active provider reads — for error messages."""
        return {
            "openrouter": "OPENROUTER_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }.get(self.llm_provider.strip().lower(), "OPENROUTER_API_KEY")

    @property
    def resolved_llm_model(self) -> str:
        """`LLM_MODEL` if set, else the provider's default.

        Defaulting per provider is what makes `LLM_PROVIDER=anthropic` work on its
        own. A single shared default cannot: the gateway wants
        `anthropic/claude-haiku-4.5` and the first-party API wants
        `claude-haiku-4-5`, and each 404s on the other's spelling — on the first
        turn of the first call.
        """
        if self.llm_model.strip():
            return self.llm_model.strip()
        return self._PROVIDER_MODELS.get(
            self.llm_provider.strip().lower(), self._PROVIDER_MODELS["openrouter"])

    # --- Models (swap to change models) ------------------------------------- #
    # The speech models are OpenRouter slugs, so they are vendor-namespaced rather
    # than the bare names a single-vendor API takes. STT_MODEL has to be a
    # transcription model and TTS_MODEL a text-to-speech one — the two endpoints
    # reject each other's models.
    #
    # whisper-large-v3 is the accuracy/price sweet spot on the board:
    # `openai/whisper-large-v3-turbo` is listed but costs ~25x more per minute.
    stt_model: str = Field(default="openai/whisper-large-v3", validation_alias="STT_MODEL")
    # Left empty on purpose — see `resolved_llm_model`. Set it to pin a model.
    llm_model: str = Field(default="", validation_alias="LLM_MODEL")
    # THE AGENT MUST SOUND LIKE ONE PERSON, and it must not leave the carrier in
    # silence. Those two requirements together pick the model — most TTS models on
    # this gateway fail one or the other. Measured on one identical load pitch
    # (~12s of speech), latency as a multiple of real time:
    #
    #   microsoft/mai-voice-2-flash   1.5s  0.12x  named voice   <- chosen
    #   microsoft/mai-voice-2         1.6s  0.14x  named voice
    #   minimax/speech-2.8-turbo      2.0s   —     mp3 only
    #   hexgrad/kokoro-82m            3.0s  0.24x  named voice
    #   fish-audio/s2.1-pro-free      3.1s  0.26x  NO NAMED VOICE
    #   deepgram/aura-2               5.1s  0.50x  named voice
    #   deepgram/flux-tts:free        7.2s  0.64x  named voice (36 of them)
    #
    # fish-audio was the original choice and is the bug being fixed here: it
    # REJECTS the `voice` parameter outright ("Provider returned 400" for any
    # value) because its voice identity comes from cloning a reference clip, not
    # from a name. With no voice it falls back to a provider-side default that is
    # NOT stable, so the speaker changed between turns of the same call. Heard on
    # a real call.
    #
    # Synthesis is NOT streamed (see `telephony.worker`), so the whole utterance is
    # generated before a single sample reaches the caller. That is why the multiple
    # of real time matters more than it looks: at 0.64x a 12-second pitch is seven
    # seconds of dead air, on top of the LLM and the endpointing delay.
    tts_model: str = Field(default="microsoft/mai-voice-2-flash",
                           validation_alias="TTS_MODEL")
    # Voices are namespaced to the provider — "alloy" is an OpenAI name, meaningless
    # to Microsoft or Deepgram. MAI-Voice-2 spells them `en-US-<Name>:MAI-Voice-2`
    # and has a SHORT roster: of 30 plausible names probed, only Harper and Ethan
    # exist. Ethan is the male one, which is who Alex is.
    #
    # Deepgram is the fallback worth knowing about, because it is the only provider
    # here that will TELL you its voices — an invalid one returns a 400 listing all
    # 36. Everything else answers an opaque "Provider returned 400".
    # `tools/audition_voices.py` renders the same line across candidates so they can
    # be compared by ear and by latency.
    tts_voice: str = Field(default="en-US-Ethan:MAI-Voice-2",
                           validation_alias="TTS_VOICE")
    # `/audio/speech` returns raw PCM with no container, and reports the rate in
    # its Content-Type (`audio/pcm;rate=24000;channels=1`). That header is what we
    # use; this is only the fallback for a response that omits it. Wrong here and
    # every reply reaches the caller chipmunked or slurred.
    #
    # And the rate really does differ by model, which is why it is read rather than
    # assumed: Deepgram Flux answers 24,000 while fish-audio answers 44,100. Either
    # default would be wrong for the other model, and the failure is not an error —
    # it is a voice at 54% speed that sounds like a bad line.
    tts_sample_rate: int = Field(default=24000, validation_alias="TTS_SAMPLE_RATE")
    # Every reply the carrier hears goes through one of these, so a hung request
    # is dead air on a live call. Short on purpose: the worker would rather raise
    # and let LiveKit drop the turn than hold the line open waiting.
    tts_timeout: float = Field(default=15.0, validation_alias="TTS_TIMEOUT")

    # Bias Whisper toward the words a carrier actually says on these calls.
    #
    # NOTE: OpenRouter's transcription endpoint documents `prompt` as accepted and
    # IGNORED, so this is not in effect today — it is still sent, costs nothing,
    # and starts working the day the gateway forwards it. Spoken load and MC
    # numbers are the accuracy risk this was mitigating; `parsing.digit_readings`
    # and the read-back confirmation are what actually catch a misheard one.
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

    # --- Deadhead ------------------------------------------------------------ #
    # Driving miles over straight-line miles. The agent estimates how far a
    # caller's empty truck is from the pickup with a great-circle distance and
    # this multiplier, then SPEAKS IT ROUNDED ("about 90 miles") because the
    # estimate is only worth about ±15%.
    #
    # 1.2 is the usual rule of thumb for the US highway network — low for the
    # mountain west, high for the plains. Raise it for a lane network that runs
    # around geography. This number never touches a rate; if deadhead is ever
    # priced, it needs real road miles from a routing engine instead.
    deadhead_road_factor: float = Field(
        default=1.2, validation_alias="DEADHEAD_ROAD_FACTOR")

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
    # `/load/search` pages at 200 rows and `perPage` is ignored, so a full board
    # read costs one request per 200 loads. This is the runaway backstop, not a
    # target: the board search stops as soon as it has enough loads to read out, so
    # a second page is only ever fetched when the first was full of loads the agent
    # can't sell. 10 pages = 2000 loads.
    transport_pro_max_search_pages: int = Field(
        default=10, validation_alias="TRANSPORT_PRO_MAX_SEARCH_PAGES")
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

    # --- Which office's freight may this deployment sell? -------------------- #
    # EMPTY = no filtering, the whole company board (the previous behaviour).
    # Set to an office's `terminalCode` and the agent may only quote and book that
    # office's loads — the office terminal PLUS every POD and team parented under
    # it. Fort Wayne Office is code "1001".
    #
    # The subtree is the point. On the live tenant Fort Wayne's own terminal holds
    # 4 posted loads and its 49 PODs hold another 338, so scoping to the office
    # alone would hide 99% of the office's freight while appearing to work.
    #
    # Resolved by CODE rather than id because a code is what a human can look up
    # and confirm in Transport Pro; the walk and the load comparison both then
    # happen on `id`, which is what a load actually carries.
    transport_pro_office_terminal_code: str = Field(
        default="", validation_alias="TRANSPORT_PRO_OFFICE_TERMINAL_CODE")
    # Manual pin of terminal IDS, comma-separated — bypasses the tree walk
    # entirely. For a deployment whose scope isn't a clean subtree, or to keep
    # running if `/terminal/search` is unavailable.
    transport_pro_office_terminal_ids: str = Field(
        default="", validation_alias="TRANSPORT_PRO_OFFICE_TERMINAL_IDS")
    # Terminal ids ADDED to the walked subtree rather than replacing it. For
    # testing against a load parked outside the office tree, which would otherwise
    # be correctly rejected. Leave unset in production.
    transport_pro_extra_terminal_ids: str = Field(
        default="", validation_alias="TRANSPORT_PRO_EXTRA_TERMINAL_IDS")
    # What to do with a load whose terminal we cannot read. Default EXCLUDE: the
    # requirement is "this office's loads only", so an unreadable one must not be
    # assumed in scope. Turn on only while checking field names against live data.
    transport_pro_allow_unknown_terminal: bool = Field(
        default=False, validation_alias="TRANSPORT_PRO_ALLOW_UNKNOWN_TERMINAL")
    # The terminal tree is org structure, not freight — it changes when somebody is
    # hired, so it is cached for far longer than a load.
    transport_pro_terminal_cache_seconds: float = Field(
        default=3600.0, validation_alias="TRANSPORT_PRO_TERMINAL_CACHE_SECONDS")

    @property
    def office_terminal_ids(self) -> frozenset[str]:
        """The manual pin, normalised to strings. Empty when unset."""
        return frozenset(
            part.strip() for part in self.transport_pro_office_terminal_ids.split(",")
            if part.strip()
        )

    @property
    def extra_terminal_ids(self) -> frozenset[str]:
        return frozenset(
            part.strip() for part in self.transport_pro_extra_terminal_ids.split(",")
            if part.strip()
        )

    @property
    def scopes_by_office(self) -> bool:
        """True when this deployment is restricted to one office's loads."""
        return bool(self.transport_pro_office_terminal_code.strip()
                    or self.office_terminal_ids)

    # --- Highway (carrier qualifications + cargo insurance) ------------------ #
    # OPTIONAL enrichment. With no token the agent vets exactly as it did before,
    # on Transport Pro alone; with one, Highway's rules_assessment overrides
    # Transport Pro's classification list in BOTH directions (see
    # `Carrier.qualifies_for`), because that list has been observed wrong each way.
    highway_api_url: str = Field(
        default="https://highway.com/core/connect/external_api/v1/carriers",
        validation_alias="HIGHWAY_API_URL")
    # Stored WITHOUT the "Bearer " prefix — the client adds it, and tolerates one
    # being present anyway. This is a JWT with a hard expiry, not a static key.
    highway_api_token: str = Field(default="", validation_alias="HIGHWAY_API_TOKEN")
    # Shorter than the Transport Pro timeout on purpose. This is an ENRICHMENT
    # call on the critical path of a live conversation: a slow Highway is worse
    # than no Highway, because the fallback (Transport Pro's own list) is already
    # a usable answer.
    highway_timeout: float = Field(default=8.0, validation_alias="HIGHWAY_TIMEOUT")
    # Prefer Highway's `dba_name` over Transport Pro's `carrier_name` for the name
    # the agent reads back. Transport Pro frequently returns a PERSON there (an
    # owner-operator's own name) while Highway has the trading name, and the agent
    # is instructed to confirm carriers by COMPANY name.
    highway_prefer_company_name: bool = Field(
        default=True, validation_alias="HIGHWAY_PREFER_COMPANY_NAME")

    @property
    def uses_highway(self) -> bool:
        """True when Highway is configured. No token = the feature is simply off."""
        return bool(self.highway_api_token.strip() and self.highway_api_url.strip())

    # --- Transport Pro "HappyRobot" endpoint --------------------------------- #
    # Same host as TRANSPORT_PRO_URL, different path, different auth (a static
    # bearer token, not the /auth login). Action-based, and the ONLY route to two
    # things `publicapi` does not expose:
    #   accept_offer   -> the carrier-facing booking link (`book_now_url`)
    #   invite_carrier -> the Highway connect invite for a NOT_CONNECTED carrier
    happyrobot_url: str = Field(default="", validation_alias="HAPPYROBOT_URL")
    happyrobot_token: str = Field(default="", validation_alias="HAPPYROBOT_TOKEN")
    happyrobot_timeout: float = Field(
        default=15.0, validation_alias="HAPPYROBOT_TIMEOUT")

    # Offer attribution, and these two are NOT interchangeable. The REST /offer
    # path accepts 4876 ("API BookMateAI") as `recordAsUserId`, while the
    # HappyRobot `send_offer` action REJECTS it outright with "tp_user_id invalid
    # or inactive" and needs 4236 ("Happy Robot Chicago"). Learned the hard way in
    # the CircleConnect email agent; kept configurable so a desk can re-attribute
    # without a code change.
    transport_pro_booking_user_id: int = Field(
        default=4876, validation_alias="TRANSPORT_PRO_BOOKING_USER_ID")
    transport_pro_log_offer_user_id: int = Field(
        default=4236, validation_alias="TRANSPORT_PRO_LOG_OFFER_USER_ID")

    @property
    def uses_happyrobot(self) -> bool:
        """True when the booking-link / invite endpoint is configured."""
        return bool(self.happyrobot_url.strip() and self.happyrobot_token.strip())

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
