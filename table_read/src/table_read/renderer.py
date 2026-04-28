"""ElevenLabs TTS client.

Async, with bounded concurrency, exponential-backoff retry on 429/5xx, and
in-flight de-duplication so concurrent renders of an identical (text,
voice_id, settings) tuple share one HTTP request.

Outputs are PCM 24kHz/24-bit wave files.  Concatenation happens in
playback.py (lossless until the final MP3 encode).
"""

from __future__ import annotations

import asyncio
import os
import random
import wave
from pathlib import Path

import httpx

from .hashing import sha256_hex, short_hash
from .models import (
    Casting,
    Direction,
    DirectionTrack,
    Lexicon,
    Manifest,
    RenderRecord,
    Screenplay,
    VoiceSettings,
)
from .caster import dsl_to_voice_settings

ELEVEN_BASE = "https://api.elevenlabs.io"
PCM_SAMPLE_RATE = 24_000  # ElevenLabs PCM output rate we request
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class ElevenLabsError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise ElevenLabsError("ELEVENLABS_API_KEY not set in environment.")
    return key


def render_hash(
    *,
    text: str,
    voice_id: str,
    voice_settings: VoiceSettings,
    seed: int,
    previous_text: str | None,
    next_text: str | None,
    tts_model_id: str,
) -> str:
    """The cache key for a single rendered line.  Any input change => re-render."""
    return sha256_hex(
        text,
        voice_id,
        voice_settings,
        seed,
        previous_text or "",
        next_text or "",
        tts_model_id,
    )


def _seed_for_line(line_id: str) -> int:
    # Deterministic per-line seed derived from line_id.  Stored in the
    # manifest so re-renders are reproducible.
    return int(short_hash(line_id, length=8), 16) % (2**31 - 1)


class ElevenLabsRenderer:
    def __init__(
        self,
        *,
        tts_model_id: str,
        concurrency: int | None = None,
        max_retries: int = 4,
    ) -> None:
        self.tts_model_id = tts_model_id
        self._sem = asyncio.Semaphore(
            concurrency
            or int(os.environ.get("TR_ELEVENLABS_CONCURRENCY", "4"))
        )
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        # in-flight dedupe: render_hash -> Future[bytes (pcm)]
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        self._inflight_lock = asyncio.Lock()

    async def __aenter__(self) -> "ElevenLabsRenderer":
        self._client = httpx.AsyncClient(
            base_url=ELEVEN_BASE,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "xi-api-key": _api_key(),
                "accept": "audio/wav",
            },
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _stream_pcm(
        self,
        *,
        voice_id: str,
        text: str,
        voice_settings: VoiceSettings,
        seed: int,
        previous_text: str | None,
        next_text: str | None,
    ) -> bytes:
        """One ElevenLabs call.  Returns PCM bytes (24kHz mono s16le).

        We request `output_format=pcm_24000` so we can concatenate without
        re-encoding.  WAV header is added at write time.
        """
        assert self._client is not None
        url = f"/v1/text-to-speech/{voice_id}"
        params = {"output_format": "pcm_24000"}
        body: dict = {
            "text": text,
            "model_id": self.tts_model_id,
            "voice_settings": voice_settings.model_dump(mode="json"),
            "seed": seed,
        }
        if previous_text:
            body["previous_text"] = previous_text
        if next_text:
            body["next_text"] = next_text

        attempt = 0
        backoff = 1.0
        while True:
            attempt += 1
            try:
                async with self._sem:
                    resp = await self._client.post(url, params=params, json=body)
                if resp.status_code == 200:
                    return resp.content
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    sleep_for = backoff + random.uniform(0, backoff)
                    await asyncio.sleep(sleep_for)
                    backoff *= 2
                    continue
                raise ElevenLabsError(
                    f"ElevenLabs {resp.status_code}: {resp.text[:300]}"
                )
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                if attempt < self.max_retries:
                    sleep_for = backoff + random.uniform(0, backoff)
                    await asyncio.sleep(sleep_for)
                    backoff *= 2
                    continue
                raise ElevenLabsError(f"ElevenLabs network error: {e}") from e

    async def _get_pcm(
        self,
        *,
        rh: str,
        voice_id: str,
        text: str,
        voice_settings: VoiceSettings,
        seed: int,
        previous_text: str | None,
        next_text: str | None,
    ) -> bytes:
        """Wrap _stream_pcm with in-flight de-dup keyed on render_hash."""
        async with self._inflight_lock:
            existing = self._inflight.get(rh)
            if existing is not None:
                return await existing
            fut: asyncio.Future[bytes] = asyncio.get_event_loop().create_future()
            self._inflight[rh] = fut

        try:
            pcm = await self._stream_pcm(
                voice_id=voice_id,
                text=text,
                voice_settings=voice_settings,
                seed=seed,
                previous_text=previous_text,
                next_text=next_text,
            )
            fut.set_result(pcm)
            return pcm
        except Exception as e:
            fut.set_exception(e)
            raise
        finally:
            async with self._inflight_lock:
                self._inflight.pop(rh, None)

    async def render_one(
        self,
        *,
        line_id: str,
        beat_id: str,
        character: str,
        voice_id: str,
        text: str,
        voice_settings: VoiceSettings,
        previous_text: str | None,
        next_text: str | None,
        out_dir: Path,
        cached_record: RenderRecord | None = None,
    ) -> RenderRecord:
        seed = _seed_for_line(line_id)
        rh = render_hash(
            text=text,
            voice_id=voice_id,
            voice_settings=voice_settings,
            seed=seed,
            previous_text=previous_text,
            next_text=next_text,
            tts_model_id=self.tts_model_id,
        )

        # Reuse on hash match.
        if (
            cached_record
            and cached_record.render_hash == rh
            and Path(cached_record.wav_path).exists()
        ):
            return cached_record

        pcm = await self._get_pcm(
            rh=rh,
            voice_id=voice_id,
            text=text,
            voice_settings=voice_settings,
            seed=seed,
            previous_text=previous_text,
            next_text=next_text,
        )

        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{line_id}_{rh[:10]}.wav"
        wav_path = out_dir / filename
        _write_pcm_as_wav(pcm, wav_path, sample_rate=PCM_SAMPLE_RATE)
        duration_ms = _pcm_duration_ms(len(pcm), sample_rate=PCM_SAMPLE_RATE)

        return RenderRecord(
            line_id=line_id,
            beat_id=beat_id,
            character=character,
            voice_id=voice_id,
            voice_settings=voice_settings,
            seed=seed,
            text=text,
            previous_text=previous_text,
            next_text=next_text,
            render_hash=rh,
            wav_path=str(wav_path),
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# WAV file I/O (we receive raw PCM bytes from ElevenLabs)
# ---------------------------------------------------------------------------


def _write_pcm_as_wav(pcm: bytes, path: Path, *, sample_rate: int) -> None:
    """Wrap raw 16-bit PCM mono bytes in a minimal RIFF/WAV container."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def _pcm_duration_ms(byte_len: int, *, sample_rate: int) -> int:
    # 16-bit mono -> 2 bytes per sample
    samples = byte_len // 2
    return int(round(1000 * samples / sample_rate))


# ---------------------------------------------------------------------------
# Orchestration helpers
# ---------------------------------------------------------------------------


def line_id_for(beat_id: str, character: str, text: str) -> str:
    return f"{beat_id}_{character.replace(' ', '_')}_{short_hash(text, length=6)}"


def _flatten_directions(
    tracks: list[DirectionTrack],
) -> list[Direction]:
    flat: list[Direction] = []
    for t in sorted(tracks, key=lambda x: x.scene_idx):
        flat.extend(t.directions)
    return flat


async def render_screenplay(
    renderer: ElevenLabsRenderer,
    *,
    screenplay: Screenplay,
    casting: Casting,
    direction_tracks: list[DirectionTrack],
    lexicon: Lexicon,
    out_dir: Path,
    cached_manifest: Manifest | None = None,
    run_uuid: str,
) -> Manifest:
    cast_by_char = casting.by_character()
    directions = _flatten_directions(direction_tracks)
    cached_by_line: dict[str, RenderRecord] = (
        {r.line_id: r for r in cached_manifest.records}
        if cached_manifest
        else {}
    )

    # First pass: build the full plan so previous_text / next_text are stable
    # before we kick off any HTTP work.
    plan: list[dict] = []
    for i, d in enumerate(directions):
        cast = cast_by_char.get(d.character)
        if cast is None:
            # Casting missing for this character: skip the line.
            continue
        prev_text = directions[i - 1].text if i > 0 else None
        next_text = directions[i + 1].text if i + 1 < len(directions) else None
        rendered_text = lexicon.apply(d.text)
        line_settings = dsl_to_voice_settings(d.dsl)
        line_id = line_id_for(d.beat_id, d.character, d.text)
        plan.append(
            {
                "line_id": line_id,
                "beat_id": d.beat_id,
                "character": d.character,
                "voice_id": cast.voice_id,
                "text": rendered_text,
                "voice_settings": line_settings,
                "previous_text": lexicon.apply(prev_text) if prev_text else None,
                "next_text": lexicon.apply(next_text) if next_text else None,
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)

    async def _render(item: dict) -> RenderRecord:
        return await renderer.render_one(
            **item,
            out_dir=out_dir,
            cached_record=cached_by_line.get(item["line_id"]),
        )

    results = await asyncio.gather(*(_render(item) for item in plan))
    return Manifest(
        run_uuid=run_uuid,
        tts_model_id=renderer.tts_model_id,
        records=results,
    )
