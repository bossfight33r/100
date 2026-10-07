"""transcribe: source.mp4 -> audio.wav (mono 16k) -> transcript.json (пословно).

Кампания с `transcribe: false` (ADR-0014) получает только audio.wav и пустой
transcript.json — без Whisper; с `selection: signals` пустая речь не ошибка.
"""

from __future__ import annotations

from typing import Any

from clipfactory.media import ffmpeg
from clipfactory.media.probe import probe
from clipfactory.pipeline.context import NoSpeechError, SourceError, StageContext, ValidationFailed
from clipfactory.schemas import SelectionMode, StageName, StageResult, Transcript


def speech_required(ctx: StageContext) -> bool:
    return ctx.campaign.selection in (SelectionMode.transcript, SelectionMode.hybrid)


class TranscribeStage:
    name = StageName.transcribe
    version = 1

    def config(self, ctx: StageContext) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "backend": ctx.backends.identity("transcriber"),
            "language": ctx.campaign.language,
        }
        # ключи только для не-дефолтных кампаний: кеш прежних job не инвалидируется
        if not ctx.campaign.transcribe:
            cfg = {"backend": "none"}
        elif not speech_required(ctx):
            cfg["speech_optional"] = True
        return cfg

    def input_keys(self, ctx: StageContext) -> list[str]:
        return [ctx.key("source.mp4")]

    def run(self, ctx: StageContext) -> StageResult:
        src = ctx.local_path(ctx.key("source.mp4"))
        info = probe(src)
        if info.audio is None:
            raise SourceError("source has no audio track — nothing to transcribe")
        wav_key = ctx.key("audio.wav")
        wav = ctx.local_path(wav_key)
        ffmpeg.ffmpeg(
            ["-i", str(src), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", "-map_metadata", "-1", "-bitexact", str(wav)],
            log_path=ctx.log_path(self.name),
            cancel=ctx.cancel,
            timeout=ctx.settings.ffmpeg_timeout_sec,
        )  # fmt: skip
        ctx.commit(wav_key, wav)

        if ctx.campaign.transcribe:
            transcript = ctx.backends.transcriber.transcribe(wav, language=ctx.campaign.language)
        else:
            transcript = Transcript(
                language=ctx.campaign.language or "und",
                model="none",
                duration=info.duration,
                words=[],
                segments=[],
            )
        if not transcript.words and speech_required(ctx):
            raise NoSpeechError("no speech detected in the source")
        out_key = ctx.key("transcript.json")
        ctx.write_model(out_key, transcript)
        return StageResult(
            stage=self.name,
            outputs=[wav_key, out_key],
            info={"language": transcript.language, "words": len(transcript.words)},
        )

    def validate(self, ctx: StageContext, outputs: list[str]) -> None:
        t = ctx.read_model(ctx.key("transcript.json"), Transcript)
        if not t.words and speech_required(ctx):
            raise ValidationFailed("transcript has no words")
        if any(w.end < w.start for w in t.words):
            raise ValidationFailed("transcript has invalid word timings")
