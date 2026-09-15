"""Transcription providers: a model, and the limits that belong to it.

The chunking rules are not properties of audio. They are properties of the
model on the other end of the call:

- `max_bytes` is the API's request cap. OpenAI rejects anything over 25MB;
  Deepgram takes 2GB.
- `max_chunk_seconds` exists because the gpt-4o transcribe models cap their
  OUTPUT near 2000 tokens and truncate silently rather than erroring, so long
  audio has to be split on duration as well as size. Whisper has no such cap,
  and Deepgram has no duration limit worth naming.
- `truncation_word_threshold` is the tell for that silent truncation. A model
  that cannot truncate sets it to None and the check is skipped entirely.

Keeping these next to the model means swapping the model swaps its limits with
it. Before this module they lived in config.py as bare constants, and setting
TRANSCRIBE_MODEL = "whisper-1" left CHUNK_SECONDS at 8 minutes: two and a half
times the API calls that model needs, at no benefit.

The config constants are still the source of the default provider's values, so
a value changed there still changes what the default provider does.

Adding a provider: subclass nothing, implement the protocol, register it in
PROVIDERS. transcribe.py asks the provider for its limits and never reads a
config constant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from intake import config

# A provider with no meaningful duration limit still needs a number, because
# the split decision is a comparison. A day of audio is not a real lecture.
NO_DURATION_LIMIT = 24 * 60 * 60


@runtime_checkable
class TranscriptionProvider(Protocol):
    """One transcription backend and the limits transcribe.py must respect."""

    #: How this provider is named in logs and in TRANSCRIBE_MODEL.
    name: str
    #: Hard request cap. A chunk over this is an error, not a retry.
    max_bytes: int
    #: Re-encode to mono 64kbps before uploading anything bigger than this.
    compress_threshold_bytes: int
    #: Split audio longer than this, whatever it weighs.
    max_chunk_seconds: int
    #: A chunk at or above this word count probably got cut off. None when
    #: the model has no output cap and so cannot truncate.
    truncation_word_threshold: int | None
    #: Whether the provider labels speakers natively. Nothing reads this yet;
    #: it is what a diarization roadmap item would turn on.
    diarization: bool
    #: The .env setting holding this provider's key, so a preflight check can
    #: ask for the right one rather than always asking for OpenAI's.
    api_key_setting: str

    def transcribe_file(self, path: Path, prompt: str = "") -> str:
        """One API call. `prompt` is optional context; providers may ignore it."""


def _too_big(provider: TranscriptionProvider, path: Path, size: int) -> RuntimeError:
    def mb(n: int) -> str:
        return f"{n / 1024 / 1024:.1f}MB"

    return RuntimeError(
        f"{path.name} is {mb(size)}, over {provider.name}'s {mb(provider.max_bytes)} "
        f"API limit even after compression and splitting"
    )


class OpenAIProvider:
    """Any endpoint shaped like OpenAI's /v1/audio/transcriptions.

    That covers OpenAI itself and the OpenAI-compatible hosts, Groq among
    them, which differ only in base URL, key, and limits.

    The defaults are the config constants, so OpenAIProvider() with no
    arguments is exactly what this program did before providers existed.
    """

    def __init__(
        self,
        model: str = "",
        *,
        name: str = "",
        base_url: str | None = None,
        api_key_setting: str = "OPENAI_API_KEY",
        max_bytes: int | None = None,
        compress_threshold_bytes: int | None = None,
        max_chunk_seconds: int | None = None,
        truncation_word_threshold: int | None = config.TRUNCATION_WORD_THRESHOLD,
        diarization: bool = False,
    ) -> None:
        self.model = model or config.TRANSCRIBE_MODEL
        self.name = name or self.model
        self.base_url = base_url
        self.api_key_setting = api_key_setting
        self.max_bytes = config.WHISPER_LIMIT_BYTES if max_bytes is None else max_bytes
        self.compress_threshold_bytes = (
            config.COMPRESS_THRESHOLD_BYTES
            if compress_threshold_bytes is None
            else compress_threshold_bytes
        )
        self.max_chunk_seconds = (
            config.CHUNK_SECONDS if max_chunk_seconds is None else max_chunk_seconds
        )
        self.truncation_word_threshold = truncation_word_threshold
        self.diarization = diarization
        self._cached_client = None

    def client(self):
        """The SDK client, built on first use so importing costs no key."""
        if self._cached_client is None:
            from openai import OpenAI

            kwargs = {"api_key": config.require(self.api_key_setting)}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._cached_client = OpenAI(**kwargs)
        return self._cached_client

    def transcribe_file(self, path: Path, prompt: str = "") -> str:
        size = path.stat().st_size
        if size > self.max_bytes:
            raise _too_big(self, path, size)
        with path.open("rb") as fh:
            kwargs = {"model": self.model, "file": fh, "response_format": "text"}
            if prompt:
                kwargs["prompt"] = prompt
            result = self.client().audio.transcriptions.create(**kwargs)
        # response_format="text" gives a plain string; object form has .text
        return (result if isinstance(result, str) else result.text).strip()


class DeepgramProvider:
    """Deepgram's prerecorded REST endpoint.

    The reason a second provider exists in this slice: nothing about Deepgram
    is OpenAI-shaped. No SDK, a different auth header, JSON out rather than
    text, 2GB rather than 25MB, and no output cap at all, so a 39 minute
    lecture is one request instead of five. If transcribe.py still has an
    OpenAI-specific assumption in it, this class is what finds it.

    Speakers are labeled natively here (`diarize=true`), which is why this is
    the provider a later diarization item would build on.
    """

    URL = "https://api.deepgram.com/v1/listen"

    def __init__(
        self,
        model: str = "nova-3",
        *,
        name: str = "",
        api_key_setting: str = "DEEPGRAM_API_KEY",
        timeout: int = 600,
    ) -> None:
        self.model = model
        self.name = name or f"deepgram/{model}"
        self.api_key_setting = api_key_setting
        self.timeout = timeout
        # Deepgram documents 2GB for a prerecorded request. Nothing a lecture
        # produces comes near it, so the split path never runs here.
        self.max_bytes = 2 * 1024 * 1024 * 1024
        # Not a limit, a courtesy: past 100MB the upload is the slow part, and
        # mono 64kbps loses nothing a speech model uses.
        self.compress_threshold_bytes = 100 * 1024 * 1024
        self.max_chunk_seconds = NO_DURATION_LIMIT
        # Deepgram streams its output; there is no token cap to truncate at.
        self.truncation_word_threshold = None
        self.diarization = True

    def transcribe_file(self, path: Path, prompt: str = "") -> str:
        import requests

        size = path.stat().st_size
        if size > self.max_bytes:
            raise _too_big(self, path, size)
        # `prompt` has no equivalent here. Deepgram's keyterm boosting takes a
        # word list, not prose, and transcribe.py sends no prompt anyway.
        response = requests.post(
            self.URL,
            params={"model": self.model, "smart_format": "true", "punctuate": "true"},
            headers={
                "Authorization": f"Token {config.require(self.api_key_setting)}",
                "Content-Type": "audio/m4a",
            },
            data=path.read_bytes(),
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"{self.name} returned {response.status_code}: "
                f"{response.text.strip()[:300]}"
            )
        payload = response.json()
        try:
            alternatives = payload["results"]["channels"][0]["alternatives"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"{self.name} returned no transcript: {payload!r}"[:300])
        return (alternatives[0].get("transcript", "") if alternatives else "").strip()


# Every provider this program knows how to reach, keyed by what you would put
# in TRANSCRIBE_MODEL. The values are factories: building one asks config for a
# key, and importing this module must not.
PROVIDERS: dict[str, callable] = {
    # The default. Cheap and accurate, and the only one here that truncates.
    "gpt-4o-mini-transcribe": lambda: OpenAIProvider("gpt-4o-mini-transcribe"),
    "gpt-4o-transcribe": lambda: OpenAIProvider("gpt-4o-transcribe"),
    # Whisper has no output cap, so it splits on size alone in practice: a
    # 20 minute chunk is a request-size choice, not a truncation guard.
    "whisper-1": lambda: OpenAIProvider(
        "whisper-1",
        max_chunk_seconds=20 * 60,
        truncation_word_threshold=None,
    ),
    # Whisper again, hosted by Groq: same model, different endpoint and key.
    # 100MB is Groq's developer-tier request cap; the free tier is 25MB.
    "groq/whisper-large-v3": lambda: OpenAIProvider(
        "whisper-large-v3",
        name="groq/whisper-large-v3",
        base_url="https://api.groq.com/openai/v1",
        api_key_setting="GROQ_API_KEY",
        max_bytes=100 * 1024 * 1024,
        compress_threshold_bytes=99 * 1024 * 1024,
        max_chunk_seconds=20 * 60,
        truncation_word_threshold=None,
    ),
    "deepgram/nova-3": lambda: DeepgramProvider("nova-3"),
    "deepgram/nova-2": lambda: DeepgramProvider("nova-2"),
}


def get(name: str | None = None) -> TranscriptionProvider:
    """The provider for `name`, or for config.TRANSCRIBE_MODEL.

    An unregistered name is assumed to be an OpenAI model, which is what
    TRANSCRIBE_MODEL meant before this module existed.
    """
    name = name or config.TRANSCRIBE_MODEL
    factory = PROVIDERS.get(name)
    return factory() if factory else OpenAIProvider(name)
