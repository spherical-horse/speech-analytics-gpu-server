# Stage 1: builder — installs deps and downloads models
FROM nvidia/cuda:12.6.1-cudnn-runtime-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Python 3.12 is not in Ubuntu 22.04 default repos — use deadsnakes PPA
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates curl \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3.12-venv \
        python3-pip gcc cmake git ffmpeg sox \
    && rm -rf /var/lib/apt/lists/*

# Install uv via system pip (Python 3.10), then use uv to create a py3.12 venv
RUN pip3 install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml .

# Create Python 3.12 venv and install all dependencies (including ML extras)
RUN uv venv /opt/venv --python python3.12
ENV PATH="/opt/venv/bin:$PATH"
RUN uv pip install ".[ml]" --extra-index-url https://download.pytorch.org/whl/cu126

# Download models — HF_TOKEN required for pyannote (license-gated)
ARG HF_TOKEN=""
ENV HF_HOME=/models
ENV HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}

RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('openai/whisper-large-v3-turbo', local_dir='/models/whisper-large-v3-turbo')"

RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('pyannote/speaker-diarization-3.1', local_dir='/models/pyannote-diarization-3.1')"

RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('pyannote/segmentation-3.0', local_dir='/models/pyannote-segmentation-3.0')"

RUN python -c "from huggingface_hub import snapshot_download; \
    snapshot_download('jonatasgrosman/wav2vec2-large-xlsr-53-russian', local_dir='/models/wav2vec2-ru')"

# -------------------------------------------------------------------
# Stage 2: runtime image
FROM nvidia/cuda:12.6.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Same PPA for consistent Python 3.12 runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv ffmpeg sox \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy venv (all Python packages) and models from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /models /models

# Copy application source
COPY . .

ENV PATH="/opt/venv/bin:$PATH"
ENV HF_HOME=/models
ENV TRANSFORMERS_CACHE=/models
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
