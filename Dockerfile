# Stage 1: builder — installs deps and downloads models
FROM nvidia/cuda:12.6.1-cudnn-runtime-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-dev python3-pip \
    gcc cmake git curl ffmpeg sox \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml .

# Install all deps including ML
RUN uv pip install --system ".[ml]" \
    --extra-index-url https://download.pytorch.org/whl/cu126

# Download models — HF_TOKEN required for pyannote (license-gated)
ARG HF_TOKEN=""
ENV HF_HOME=/models
ENV HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}

RUN python3.12 -c "from huggingface_hub import snapshot_download; \
    snapshot_download('openai/whisper-large-v3-turbo', local_dir='/models/whisper-large-v3-turbo')"

RUN python3.12 -c "from huggingface_hub import snapshot_download; \
    snapshot_download('pyannote/speaker-diarization-3.1', use_auth_token='${HF_TOKEN}', local_dir='/models/pyannote-diarization-3.1')"

RUN python3.12 -c "from huggingface_hub import snapshot_download; \
    snapshot_download('pyannote/segmentation-3.0', use_auth_token='${HF_TOKEN}', local_dir='/models/pyannote-segmentation-3.0')"

RUN python3.12 -c "from huggingface_hub import snapshot_download; \
    snapshot_download('jonatasgrosman/wav2vec2-large-xlsr-53-russian', local_dir='/models/wav2vec2-ru')"

# -------------------------------------------------------------------
# Stage 2: runtime image
FROM nvidia/cuda:12.6.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3-pip ffmpeg sox \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/dist-packages /usr/local/lib/python3.12/dist-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy downloaded models
COPY --from=builder /models /models

# Copy application source
COPY . .

ENV HF_HOME=/models
ENV TRANSFORMERS_CACHE=/models
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
