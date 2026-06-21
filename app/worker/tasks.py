from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select, update

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.models import Task
from app.worker import pipeline

log = structlog.get_logger()


async def _set_progress(task_id: str, progress: int) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(update(Task).where(Task.id == uuid.UUID(task_id)).values(progress=progress))
        await session.commit()


async def _fail_task(task_id: str, error_code: str, error_message: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Task)
            .where(Task.id == uuid.UUID(task_id))
            .values(
                status="failed",
                error_code=error_code,
                error_message=error_message,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()


def _run_pipeline(task_id: str, audio_path: str, work_dir: str) -> dict:
    """Blocking ML pipeline — runs in thread executor."""
    import torch
    import whisperx

    whisper_model, diarization_pipeline, align_model, align_metadata = pipeline.get_models()
    device = "cuda"

    # ffprobe duration check
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"INVALID_AUDIO:{result.stderr.strip()}")

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ValueError(f"INVALID_AUDIO:Cannot parse duration") from exc

    if duration > settings.MAX_DURATION_SECONDS:
        raise ValueError(f"TOO_LONG:Audio duration {duration:.0f}s exceeds {settings.MAX_DURATION_MIN} minutes")

    # WhisperX transcription
    audio = whisperx.load_audio(audio_path)
    transcribe_result = whisper_model.transcribe(
        audio,
        batch_size=settings.WHISPER_BATCH_SIZE,
        language=settings.WHISPER_LANGUAGE,
    )

    # Diarization — may fail gracefully
    diarization_failed = False
    diarize_segments = None
    try:
        diarize_segments = diarization_pipeline(
            audio_path,
            min_speakers=settings.MIN_SPEAKERS,
            max_speakers=settings.MAX_SPEAKERS,
        )
    except Exception as exc:
        log.warning("diarization_failed", task_id=task_id, error=str(exc))
        diarization_failed = True

    # Alignment
    aligned = whisperx.align(
        transcribe_result["segments"],
        align_model,
        align_metadata,
        audio,
        device,
        return_char_alignments=False,
    )

    # Assign speakers
    if diarize_segments is not None and not diarization_failed:
        final = whisperx.assign_word_speakers(diarize_segments, aligned)
    else:
        final = aligned

    # Build output JSON
    segments_out = []
    speakers_seen: set[str] = set()

    for seg in final.get("segments", []):
        speaker = seg.get("speaker", "UNKNOWN") or "UNKNOWN"
        if speaker != "UNKNOWN":
            speakers_seen.add(speaker)

        words_out = []
        for w in seg.get("words", []):
            words_out.append(
                {
                    "word": w.get("word", ""),
                    "start": round(float(w.get("start", 0.0)), 3),
                    "end": round(float(w.get("end", 0.0)), 3),
                    "score": round(float(w.get("score", 0.0)), 4),
                    "speaker": w.get("speaker", speaker) or speaker,
                }
            )

        segments_out.append(
            {
                "start": round(float(seg.get("start", 0.0)), 3),
                "end": round(float(seg.get("end", 0.0)), 3),
                "text": seg.get("text", "").strip(),
                "speaker": speaker,
                "words": words_out,
            }
        )

    return {
        "language": settings.WHISPER_LANGUAGE,
        "duration": round(duration, 3),
        "speakers": sorted(speakers_seen) if speakers_seen else [],
        "segments": segments_out,
        "_diarization_failed": diarization_failed,
    }


async def transcribe_task(ctx: dict, task_id: str, ext: str) -> None:
    bound_log = log.bind(task_id=task_id)

    upload_path = Path(settings.UPLOADS_DIR) / f"{task_id}.{ext}"
    work_dir = Path(settings.TMP_DIR) / task_id
    audio_path = work_dir / f"input.{ext}"

    try:
        # Move file to work dir
        work_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(upload_path), str(audio_path))
        await _set_progress(task_id, 5)

        # Mark processing
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Task).where(Task.id == uuid.UUID(task_id)).values(status="processing", progress=5)
            )
            await session.commit()

        bound_log.info("task_processing_started")

        # Run blocking ML in thread
        loop = asyncio.get_event_loop()
        try:
            transcript = await loop.run_in_executor(
                None, _run_pipeline, task_id, str(audio_path), str(work_dir)
            )
        except ValueError as exc:
            parts = str(exc).split(":", 1)
            error_code = parts[0] if parts[0] in ("INVALID_AUDIO", "TOO_LONG") else "INVALID_AUDIO"
            error_message = parts[1] if len(parts) > 1 else str(exc)
            await _fail_task(task_id, error_code, error_message)
            bound_log.error("task_failed", error_code=error_code)
            return

        diarization_failed = transcript.pop("_diarization_failed", False)
        final_error_code = "DIARIZATION_FAILED" if diarization_failed else None

        await _set_progress(task_id, 95)

        # Save result
        async with AsyncSessionLocal() as session:
            task_result = await session.get(Task, uuid.UUID(task_id))
            if task_result is None:
                return

            task_result.status = "completed"
            task_result.progress = 100
            task_result.transcript_data = transcript
            task_result.completed_at = datetime.now(timezone.utc)
            if diarization_failed:
                task_result.error_code = "DIARIZATION_FAILED"
                task_result.error_message = "Diarization failed; transcript saved without speaker labels"
            await session.commit()

            has_webhook = bool(task_result.webhook_url)

        bound_log.info("task_completed", diarization_failed=diarization_failed)

        if has_webhook:
            from app.core.redis import get_arq_pool

            pool = await get_arq_pool()
            await pool.enqueue_job("deliver_webhook", task_id, 1)

    except Exception as exc:
        import torch

        error_str = str(exc)
        if isinstance(exc, torch.cuda.OutOfMemoryError):  # type: ignore[attr-defined]
            bound_log.error("task_oom")
            await _fail_task(task_id, "OOM_ERROR", f"GPU out of memory: {error_str}")
            # Do NOT re-raise — arq would retry, which we don't want for OOM
            return

        bound_log.error("task_unknown_error", exc=error_str)
        await _fail_task(task_id, "UNKNOWN_ERROR", error_str)
        raise  # Let arq retry for transient errors

    finally:
        # Always clean up temp files
        upload_path.unlink(missing_ok=True)
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


async def cleanup_expired_tasks(ctx: dict) -> None:
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                DELETE FROM tasks
                WHERE status IN ('completed', 'failed')
                  AND COALESCE(completed_at, created_at) < NOW() - INTERVAL '24 hours'
                """
            )
        )
        await session.commit()
        log.info("cleanup_expired_tasks", deleted=result.rowcount)
