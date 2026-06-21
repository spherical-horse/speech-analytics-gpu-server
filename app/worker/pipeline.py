from __future__ import annotations

import structlog

from app.core.config import settings

log = structlog.get_logger()

_whisper_model = None
_diarization_pipeline = None
_align_model = None
_align_metadata = None


def load_models() -> None:
    global _whisper_model, _diarization_pipeline, _align_model, _align_metadata

    import torch
    import whisperx
    from pyannote.audio import Pipeline

    device = "cuda"
    log.info("loading_whisper_model", model=settings.WHISPER_MODEL, compute_type=settings.WHISPER_COMPUTE_TYPE)
    _whisper_model = whisperx.load_model(
        settings.WHISPER_MODEL,
        device=device,
        compute_type=settings.WHISPER_COMPUTE_TYPE,
        language=settings.WHISPER_LANGUAGE,
    )

    log.info("loading_diarization_pipeline", model=settings.DIARIZATION_MODEL)
    _diarization_pipeline = Pipeline.from_pretrained(
        settings.DIARIZATION_MODEL,
        use_auth_token=settings.HF_TOKEN,
    ).to(torch.device(device))

    log.info("loading_alignment_model", language=settings.WHISPER_LANGUAGE)
    _align_model, _align_metadata = whisperx.load_align_model(
        language_code=settings.WHISPER_LANGUAGE,
        device=device,
    )

    log.info("all_models_loaded")


def get_models():
    return _whisper_model, _diarization_pipeline, _align_model, _align_metadata


def models_loaded() -> bool:
    return _whisper_model is not None
