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
    # Sonnet 5; only the spelling differs. OpenRouter namespaces by vendor with a
    # dotted version, Anthropic uses a hyphenated id and no date suffix.
    #
    # SONNET 5 IS THE LOW-LATENCY CHOICE HERE, which is not the obvious answer.
    # Per call it is twice as slow as Haiku 4.5 — but a reply that names money the
    # engine did not sanction is rejected and re-prompted, and three rejections
    # hand the call to a rep. What the caller waits on is per-call latency times
    # attempts. Measured over 8 turns each with `tools/measure_latency.py`, on the
    # commonest turn there is (hold at our number against a higher ask):
    #
    #   model              per call   complies   effective   ends at a rep
    #   haiku-4.5             1.82s     1 of 8       4.81s        67%
    #   sonnet-5              3.23s     8 of 8       3.23s         0%
    #
    # Haiku's failure is abbreviation, and it survived an explicit instruction not
    # to do it: it writes "what's driving the 26 on this one?" for $2600. The
    # guardrail is right to reject that — the voice would say "twenty-six" and the
    # carrier would hear twenty-six dollars. Sonnet 5 wrote "$2600" every time.
    #
    # Haiku is still the cheaper model and its pass rate swung 1-4 of 8 across
    # runs, so it is a reasonable choice for a desk watching spend: set
    # LLM_MODEL=anthropic/claude-haiku-4.5. Do NOT reach for Opus 5 for this —
    # thinking is ON by default there, which is latency this call cannot afford,
    # and LLM_MAX_TOKENS caps thinking and speech together so turns would truncate.
    _PROVIDER_MODELS = {
        "openrouter": "anthropic/claude-sonnet-5",
        "anthropic": "claude-sonnet-5",
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

    # --- Speech on the phone line: which service hears and speaks ------------- #
    # inference  — LiveKit Inference, the default. Streams over a WebSocket using
    #              the LiveKit credentials already required above: no extra key,
    #              billed on the LiveKit account. Transcription runs WHILE the
    #              caller talks and the voice starts playing on its first few
    #              hundred milliseconds of audio. That overlap is the whole
    #              difference between this path and the one below.
    # openrouter — the original path: Whisper as one HTTP POST per utterance sent
    #              only after the caller stops, and a voice that generates the
    #              entire reply before its first byte. Kept as the fallback, and
    #              still what practice mode uses (STT_MODEL / TTS_MODEL below).
    #
    # Measured on the live pipeline before the switch, per caller turn and none of
    # it overlapping: 0.55s for the VAD to close, 1.0-1.8s for Whisper to answer,
    # then 1.0-2.0s before the voice produced its first byte. On the four most
    # recent recordings 35-48% of the call was silence. Streaming removes the
    # first two waits almost entirely and cuts the third to a few hundred ms.
    stt_provider: str = Field(default="inference", validation_alias="STT_PROVIDER")
    # A LiveKit Inference model, "provider/model". Both candidates stream, emit
    # interim results (what lets the turn detector and barge-in act on partial
    # speech) and take a keyterm list that actually reaches the recogniser — the
    # Whisper `prompt` never did through OpenRouter. AssemblyAI is the default
    # because of how it WRITES NUMBERS: measured on ten phone-band lines with
    # engine noise, its formatted output parsed 10/10 — "2450", "611349", and a
    # load number read in groups ("twenty-five, thirteen, four forty-six") as
    # 2513446 — where Deepgram's numerals mode wrote a rate as "24 50" (unparsed)
    # and its smart format wrote "$24.75" for twenty-four seventy-five. It also
    # produced 3-5x more interim transcripts per second. `deepgram/nova-3` is the
    # alternative; `telephony.worker._stt_extra_kwargs` has the per-provider
    # options and the measurements.
    #
    # `assemblyai/u3-rt-pro` replaced `assemblyai/universal-streaming` on
    # 2026-09-03. The older model lost the FIRST short word of an utterance that
    # followed a long quiet stretch — reproduced offline from live recordings:
    # "Fort Wayne, Indiana at 10 am" came back as "In Indiana at 10 a.m.", "It's
    # 299953" as "299953", a quiet "sure" as "Sheila" or nothing. On the same
    # clips u3-rt-pro returned every word. Same formatting (digits, "10 AM"), so
    # the parser is unchanged.
    stt_inference_model: str = Field(default="assemblyai/u3-rt-pro",
                                     validation_alias="STT_INFERENCE_MODEL")
    # Extra vocabulary for the recogniser, comma-separated, ADDED to the built-in
    # freight list in `telephony.worker.STT_KEYTERMS`. Words and short phrases —
    # "Circle Logistics", "rate con" — not sentences and not digit strings.
    stt_keyterms: str = Field(default="", validation_alias="STT_KEYTERMS")

    tts_provider: str = Field(default="inference", validation_alias="TTS_PROVIDER")
    # "provider/model" plus a voice that model offers. The voice is REQUIRED to be a
    # fixed, named one for the same reason TTS_VOICE is below: the agent has to
    # sound like one person for the whole call, and a provider-side default is not
    # guaranteed stable. A wrong voice fails at worker start, in prewarm, not on a
    # call. Measured through this project's gateway on a 7-12s load pitch, time
    # to FIRST audio (the number the caller waits on, the rest plays while it
    # streams) — all male voices by pitch, all verified to render:
    #
    #   cartesia/sonic-3   a167e0f3-df7e-4d52-a9c3-f949145efdab   0.38s   <- default
    #   cartesia/sonic-3   820a3788-2b37-4d21-847a-b65d8a68c99a   0.41s
    #   cartesia/sonic-3   729651dc-c6c3-4ee5-97fa-350da1f88600   0.55s
    #   elevenlabs/eleven_flash_v2_5   pNInz6obpgDQGcFmaJgB       0.28s   fastest; dearer
    #   deepgram/aura-2    aura-2-orion-en                        0.28s
    #
    # Against 1.0-2.0s before the first byte on the OpenRouter voice. Cartesia is
    # the default on price; ElevenLabs Flash is the pick if the desk wants the
    # last 100ms and accepts the per-character rate. Compare by ear with
    # `tools/audition_voices.py --inference`.
    tts_inference_model: str = Field(default="cartesia/sonic-3",
                                     validation_alias="TTS_INFERENCE_MODEL")
    tts_inference_voice: str = Field(default="a167e0f3-df7e-4d52-a9c3-f949145efdab",
                                     validation_alias="TTS_INFERENCE_VOICE")

    @property
    def stt_on_inference(self) -> bool:
        return self.stt_provider.strip().lower() == "inference"

    @property
    def tts_on_inference(self) -> bool:
        return self.tts_provider.strip().lower() == "inference"

    @property
    def needs_openrouter(self) -> bool:
        """True while any AI hop on the phone line still runs through OpenRouter.

        With both speech legs on LiveKit Inference and LLM_PROVIDER=anthropic, a
        deployment needs no OpenRouter key at all; the worker's startup check reads
        this rather than demanding one unconditionally.
        """
        return (self.llm_provider.strip().lower() == "openrouter"
                or not self.stt_on_inference or not self.tts_on_inference)

    # --- Models (swap to change models) ------------------------------------- #
    # The speech models below are OpenRouter slugs, so they are vendor-namespaced
    # rather than the bare names a single-vendor API takes. STT_MODEL has to be a
    # transcription model and TTS_MODEL a text-to-speech one — the two endpoints
    # reject each other's models. They drive PRACTICE MODE (one-shot HTTP legs from
    # the dashboard) and the phone line only when STT_PROVIDER / TTS_PROVIDER is
    # set back to `openrouter`.
    #
    # Measured with `tools/measure_latency.py` on synthesised clean audio, two
    # lines a carrier actually says. Latency is a median of 3; the transcript is
    # what the model heard:
    #
    #   line          model                    lat    heard
    #   rate spoken   whisper-large-v3         1.80s  "24-75"        <- unparseable
    #   rate spoken   whisper-large-v3-turbo   1.27s  "2475"         <- chosen
    #   MC number     whisper-large-v3         1.05s  "611-349-6113" <- hallucinated
    #   MC number     whisper-large-v3-turbo   1.98s  "6-1-1-3-4-9"
    #
    # turbo is chosen on ACCURACY as much as speed, and the rate line is why: a
    # rate the transcriber mangles is a rate the negotiator never sees, which is
    # exactly how a live call on load 2513446 ended at a human. large-v3 also
    # padded a six-digit MC into a ten-digit phone number on one run, which
    # `heard_digits` then cannot match to any carrier; turbo's spelled-out form
    # strips cleanly to 611349.
    #
    # THE COST IS REAL: turbo is ~25x more per minute of audio on this gateway,
    # and audio minutes are the whole bill for STT. Set STT_MODEL back to
    # `openai/whisper-large-v3` if that outweighs the mis-heard rates for you —
    # `parsing.extract_money` now reads "24-75" and "24/75" either way, so this is
    # a margin-of-safety choice rather than a broken-vs-working one.
    #
    # Both numbers move a lot run to run; re-measure before trusting either.
    stt_model: str = Field(default="openai/whisper-large-v3-turbo",
                           validation_alias="STT_MODEL")
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
    # On this path the provider generates the WHOLE utterance before its first byte
    # (the body is then streamed — see `voice.tts.stream_pcm`), so the multiple of
    # real time matters more than it looks: at 0.64x a 12-second pitch is seven
    # seconds of dead air, on top of the LLM and the endpointing delay. This is the
    # 1-2s per-request floor that TTS_PROVIDER=inference exists to remove.
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
    #
    # MIN applies when the turn detector is CONFIDENT the caller finished; MAX
    # when it is not. The gap between them is the single largest thing the caller
    # waits on — bigger than the model, bigger than the voice.
    #
    # MAX was 8.0, and on a measured live call 5 turns out of 11 spent every one
    # of those seconds. In all five the caller had plainly finished ("Okay.", at
    # an end-of-turn probability of 0.05) — the detector was merely unsure, and
    # eight seconds of silence bought nothing on any of them. 3.0 is still a long
    # pause by phone standards and cuts up to 5 seconds off roughly half the
    # turns. Raise it back toward 6-8 if callers start getting clipped
    # mid-sentence; that is the failure this number guards against.
    #
    # MIN was 1.3 for as long as Whisper ran as one request per utterance: the
    # transcript took 1.0-1.8s to come back after the caller stopped, the turn
    # could not end before it did, and so MIN was never the thing being waited on.
    # With STT_PROVIDER=inference the transcript is in hand when the caller stops
    # and MIN became the largest single post-speech wait. The framework's own
    # default is 0.5 (0.3 for a streaming turn detector). MAX still applies
    # whenever the hosted turn detector reads the caller as mid-thought — "my MC
    # is six one one..." — and that, not MIN, is what protects digit dictation.
    #
    # 0.7 was tried first and clipped a caller on the third live call: "Looking
    # for load" was finalised at their pause, the detector read it as a complete
    # sentence, and the number arrived as a second transcript right after the
    # commit — the framework logged "transcript arrives after turn has been
    # committed, consider raising min_delay". 1.0 is the compromise; the STT's
    # own end-of-turn eagerness is the other half of that fix (see
    # `telephony.worker._stt_extra_kwargs`). If callers still get clipped, raise
    # this first.
    min_endpointing_delay: float = Field(default=1.0, validation_alias="MIN_ENDPOINTING_DELAY")
    max_endpointing_delay: float = Field(default=3.0, validation_alias="MAX_ENDPOINTING_DELAY")

    # Dead-air filler: when a reply takes longer than this to compose, the agent
    # immediately speaks a short pre-synthesized acknowledgment ("Alright, one
    # sec.") while the real reply is written. Composition measures ~3.4s on the
    # shipped model, and a caller sitting in that silence says "hello?" — which
    # used to cut off the very reply they were waiting for. The clips are made
    # once at worker start with the configured voice, carry no facts and no
    # numbers, and are deliberately NOT part of the transcript record. 0 turns
    # the feature off.
    filler_delay: float = Field(default=0.8, validation_alias="FILLER_DELAY")

    # Barge-in: how much CONTINUOUS caller speech cancels the agent's audio
    # mid-play. The library default (0.5s) meant a caller's "hello?" — spoken to
    # fill the dead air while the reply was still being synthesized — cut off
    # the very answer they were waiting for, twice in a row on one observed
    # call. 0.9s lets a short line-check pass over the top without killing
    # playback, while a caller genuinely talking over the agent still
    # interrupts in under a second.
    min_interruption_duration: float = Field(
        default=0.9, validation_alias="MIN_INTERRUPTION_DURATION")
    # An "interruption" that never produces a transcript (a cough, a passing
    # horn) resumes the cut line after this long instead of leaving dead air.
    resume_false_interruption: bool = Field(
        default=True, validation_alias="RESUME_FALSE_INTERRUPTION")
    false_interruption_timeout: float = Field(
        default=2.0, validation_alias="FALSE_INTERRUPTION_TIMEOUT")
    # A one-word backchannel while the agent is talking — "yeah", "okay", "mm-hm"
    # — is agreement, not an interruption, and it used to cut the pitch off. The
    # streaming STT's interim transcript is what makes counting words possible:
    # detected speech only counts as a barge-in once this many words have been
    # heard, and a burst that never gets there is a false interruption (the cut
    # line resumes). A caller who actually objects reaches two words at once —
    # "no wait", "hang on". 0 disables the check.
    min_interruption_words: int = Field(
        default=2, validation_alias="MIN_INTERRUPTION_WORDS")
    # The caller spoke — the VAD heard them — but no transcript ever arrived, so
    # no turn happened and the agent said nothing. Observed on the first live call
    # of the streaming pipeline: four half-second, quiet "10 AM"s in a row, each
    # returned by the recogniser as an EMPTY transcript, each met with silence,
    # and the caller hung up. After this many seconds of nothing following the
    # caller's speech the agent asks them to say it again (a pre-rendered line,
    # not part of the dialogue). Must clear MAX_ENDPOINTING_DELAY plus the
    # transcript delay, or a slow-but-successful turn would be talked over. 0
    # disables.
    unheard_reask_delay: float = Field(
        default=4.5, validation_alias="UNHEARD_REASK_DELAY")
    # White noise (dBFS, RMS) mixed into the audio the RECOGNISER gets — nothing
    # else hears it. After noise cancellation the line between the caller's
    # utterances is digital zero, and AssemblyAI's streaming speech detector goes
    # idle on that: after 15-20s of it the first ~0.3-0.5s of the next utterance
    # is lost ("Transfer it" -> "Sfer it"; a short "It's 299953" -> nothing, and
    # the agent sat silent). Reproduced offline from the recordings; -55 dBFS in
    # the gap cured it, and that is 35 dB under quiet speech. 0 disables. See
    # `telephony.worker.CarrierAgent.stt_node`.
    stt_comfort_noise_dbfs: float = Field(
        default=-55.0, validation_alias="STT_COMFORT_NOISE_DBFS")
    # Diagnostic: write the exact audio the recogniser was handed, per call, to
    # call_recordings/<call>.stt_feed.wav — so a transcript that lost words can be
    # checked against what the recogniser actually received, offline. Off unless
    # a hearing problem is being chased; it is a second recording of the caller.
    stt_feed_dump: bool = Field(default=False, validation_alias="STT_FEED_DUMP")
    # A caller who says nothing after the agent's question. After this many
    # seconds of silence the agent asks "you still there?"; after as many again it
    # says goodbye, records the call as abandoned and hangs up. A real caller who
    # set the phone down costs agent minutes for as long as the line stays open;
    # observed: a caller went quiet for forty seconds and the agent sat mute. 0
    # disables both.
    idle_prompt_seconds: float = Field(default=12.0, validation_alias="IDLE_PROMPT_SECONDS")
    idle_close_seconds: float = Field(default=12.0, validation_alias="IDLE_CLOSE_SECONDS")
    # How long after the closing line has finished playing the worker ends the
    # call. A desk hangs up after "have a good one"; leaving the line open bills
    # minutes and leaves the caller wondering. 0 keeps the line open.
    hangup_after_close_seconds: float = Field(
        default=1.5, validation_alias="HANGUP_AFTER_CLOSE_SECONDS")
    # Where this desk is, as a place the city table knows ("Fort Wayne, IN"). The
    # towns around it become recogniser vocabulary (`geo.region_keyterms`): a
    # caller's "Columbia City" is then a word the recogniser expects rather than
    # one it guesses at. Empty disables.
    office_location: str = Field(default="", validation_alias="OFFICE_LOCATION")
    stt_region_keyterms_miles: float = Field(
        default=150.0, validation_alias="STT_REGION_KEYTERMS_MILES")
    stt_region_keyterms_max: int = Field(default=60, validation_alias="STT_REGION_KEYTERMS_MAX")
    # How many calls this worker takes at once. Each call runs its own voice
    # detector, noise cancellation and audio encoding, and on Windows every call
    # shares one Python process, so a fifth caller degrades the first four rather
    # than waiting. At the cap the worker tells LiveKit it is full and new calls
    # are routed elsewhere (or ring unanswered if there is no other worker —
    # honest, and visible). 0 hands the decision back to the framework's CPU
    # measure, which in dev mode means no limit at all.
    max_concurrent_calls: int = Field(default=4, validation_alias="MAX_CONCURRENT_CALLS")

    # VAD: how confidently (threshold) and how long (seconds) someone must speak
    # before it counts as speech at all. The Silero defaults (0.5 / 0.05s) are
    # too twitchy for a phone line — background noise reads as turns. But the
    # first hand tuning (0.6 / 0.2s) proved too deaf the other way: on a live
    # call a soft one-syllable "Sure." never triggered the VAD at all, so the
    # agent sat silent on an answer it had asked for. These defaults sit between.
    # If the agent starts replying to line noise, raise them toward 0.6 / 0.2;
    # if it misses short answers ("yes", "sure"), lower them toward the Silero
    # defaults.
    vad_activation_threshold: float = Field(
        default=0.55, validation_alias="VAD_ACTIVATION_THRESHOLD")
    vad_min_speech_duration: float = Field(
        default=0.10, validation_alias="VAD_MIN_SPEECH_DURATION")

    # --- Practice mode (dashboard pitch trainer) ------------------------------ #
    # The customer a rep practices against is played by the same configured
    # composer model — no separate key, no separate provider. These knobs only
    # bound a session; the mood itself lives in the profile TOML files under
    # `practice/data/profiles/`.
    #
    # Hard cap on REP turns in one session. Past forty exchanges it isn't
    # practice anymore — and every turn is a paid model call, so this is also
    # the cost ceiling on a tab somebody forgot to close.
    practice_max_turns: int = Field(default=40, validation_alias="PRACTICE_MAX_TURNS")
    # Token budget for one customer line. Same order as LLM_MAX_TOKENS and for
    # the same reason: a person on a phone speaks in sentences, and a budget
    # this size simply cannot fit a monologue.
    practice_reply_max_tokens: int = Field(
        default=220, validation_alias="PRACTICE_REPLY_MAX_TOKENS")
    # The judge writes a full scorecard — eight scored dimensions with quotes,
    # strengths, improvements, a summary — in one JSON reply. Unlike a spoken
    # turn nobody is waiting on the line for it, so the budget and the timeout
    # are sized for completeness, not latency. A truncated verdict is retried
    # once with a brevity instruction (truncated JSON never parses).
    #
    # 4000, measured: the first live scorecard ran ~2100 tokens and was cut off
    # at a 2000 budget — TWICE, because even the brevity retry couldn't fit.
    # Don't shave this to save pennies; a judge that can't finish its sentence
    # scores nothing at all.
    practice_judge_max_tokens: int = Field(
        default=4000, validation_alias="PRACTICE_JUDGE_MAX_TOKENS")
    practice_judge_timeout: float = Field(
        default=60.0, validation_alias="PRACTICE_JUDGE_TIMEOUT")
    # The VOCAL-delivery judge — tone, clarity, energy, pace, warmth — needs a
    # model that accepts audio input, which no Claude model does, so this is the
    # one place practice reaches past the composer's provider. Chosen by probe
    # (2026-08-18): of 38 audio-input models on the gateway, gemini-3.7-flash
    # returned accurate, prosody-specific verdicts at flash-class pricing
    # (~$0.002/session). Same OpenRouter key as everything else. Set EMPTY to
    # turn vocal judging off; the conversational scorecard is unaffected.
    practice_delivery_model: str = Field(
        default="google/gemini-3.7-flash", validation_alias="PRACTICE_DELIVERY_MODEL")
    # Practice voice turns are RECORDINGS OF YOUR REPS. By default each clip
    # lives only until its session is scored, then is deleted. Set true to keep
    # them on disk (under practice_audio/ next to the DB) — a deliberate choice
    # a desk should make consciously, not inherit.
    practice_keep_audio: bool = Field(
        default=False, validation_alias="PRACTICE_KEEP_AUDIO")

    # --- Call recording -------------------------------------------------------#
    # Record real phone calls, both sides time-aligned, using livekit-agents'
    # built-in session recorder — no custom code touches the live audio path.
    # Each finished call lands as `call_recordings/<call_id>.ogg` next to the
    # DB and plays in the dashboard's Runs drawer.
    #
    # OFF BY DEFAULT, deliberately, for two reasons a desk must weigh before
    # flipping it:
    #  * CONSENT. Several US states require ALL parties to consent to a
    #    recording. Add a "this call may be recorded" line to the greeting
    #    before enabling — that wording is a business decision, not a default.
    #  * RETENTION. On LiveKit Cloud the SDK also uploads the recording to
    #    LiveKit's session observability at call end (their dashboard shows
    #    it). LiveKit already carries the raw call audio either way, but a
    #    stored copy there is a retention decision to make knowingly.
    record_calls: bool = Field(default=False, validation_alias="RECORD_CALLS")

    # --- Practice report email ----------------------------------------------- #
    # The scored report goes to the rep's account manager (picked at session
    # start, from practice/data/managers.toml). This is the codebase's FIRST
    # outbound email — everything email-shaped before it was address parsing —
    # and it is gated the way every integration is: unset SMTP_HOST/SMTP_FROM
    # and reports are still scored, stored and shown, just never mailed (the
    # skip is recorded on the report, not silent). Sending uses the stdlib;
    # there is no mail-provider dependency to configure beyond these.
    smtp_host: str = Field(default="", validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=587, validation_alias="SMTP_PORT")
    smtp_username: str = Field(default="", validation_alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", validation_alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="", validation_alias="SMTP_FROM")
    # STARTTLS on the standard submission port is the overwhelming default;
    # turn off only for a trusted internal relay (or a local test sink).
    smtp_starttls: bool = Field(default=True, validation_alias="SMTP_STARTTLS")

    @property
    def uses_practice_email(self) -> bool:
        return bool(self.smtp_host.strip() and self.smtp_from.strip())

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
    # Rehearsal loads. Each id listed here is treated as THIS desk's, posted and
    # open, whatever Transport Pro says about its terminal, posting or status —
    # so the whole call, the notes on the load included, can be walked through
    # on a dummy load before a real one. The rates and requirements still come
    # from Transport Pro. Logged as a WARNING at every lookup; leave it empty on
    # a production desk.
    transport_pro_test_load_ids: str = Field(default="", validation_alias="TEST_LOAD_IDS")

    @property
    def office_terminal_ids(self) -> frozenset[str]:
        """The manual pin, normalised to strings. Empty when unset."""
        return frozenset(
            part.strip() for part in self.transport_pro_office_terminal_ids.split(",")
            if part.strip()
        )

    @property
    def test_load_ids(self) -> frozenset[str]:
        """Rehearsal load ids (digits only), or empty."""
        return frozenset(
            "".join(ch for ch in part if ch.isdigit())
            for part in self.transport_pro_test_load_ids.split(",")
            if part.strip())

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
    # Who a call gets handed to, as a TOML file the desk edits — see
    # reps.toml.example and `lanevoice.reps`. A relative path is taken next to the
    # database. When the file exists it replaces the `reps` table at every worker
    # and dashboard start; when it does not, the table is left as it is — EMPTY
    # on a live deployment, where the agent then names the load's Transport Pro
    # rep or logs a callback. The invented sample reps are never written to a
    # live database.
    reps_file: str = Field(default="reps.toml", validation_alias="REPS_FILE")
    # Whether the worker actually moves the call when the agent hands off. The
    # agent resolves the load's rep and announces the handoff either way; with
    # this OFF (the default) the call is left where it is and the audit log says
    # the transfer was requested, nothing more — the state a desk is in until the
    # trunk allows SIP REFER (docs/LIVE_SETUP.md B5). Turn it on and the worker
    # sends the REFER to the rep's number after the handoff line is spoken.
    sip_transfer_enabled: bool = Field(
        default=False, validation_alias="SIP_TRANSFER_ENABLED")

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
