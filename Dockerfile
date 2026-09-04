FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt constraints-docker.txt ./
# Use CPU-only PyTorch to reduce image size (open-clip-torch dependency).
# A single install with -c constraints-docker.txt pins torch/torchvision to
# their +cpu variants, so open-clip-torch's transitive `torch>=2.0` dependency
# resolves against the CPU wheel instead of a second pass re-resolving it
# from PyPI's default (CUDA) build.
RUN pip install --no-cache-dir --prefix=/install \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -c constraints-docker.txt \
    -r requirements.txt

FROM python:3.13-slim AS runtime
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY flightrisk/ flightrisk/
COPY eval_data/ eval_data/
COPY pytest.ini .

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5555/api/health'); r.raise_for_status()"

ENV FLIGHTRISK_SOURCE=webcam
ENV FLIGHTRISK_PORT=5555
# Reserved for flightrisk.dashboard.__main__ --source flag (PR #26)
ENV FLIGHTRISK_MAVLINK_ADDRESS=udp://:14540
# For production: use wss:// with FLIGHTRISK_EDGE_WS_TOKEN
ENV FLIGHTRISK_EDGE_WS=ws://localhost:9000
EXPOSE 5555

CMD ["python", "-m", "flightrisk.dashboard"]
