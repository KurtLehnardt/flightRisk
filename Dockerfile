FROM python:3.13-slim AS builder
WORKDIR /app
COPY requirements.txt .
# Use CPU-only PyTorch to reduce image size (open-clip-torch dependency)
RUN pip install --no-cache-dir --prefix=/install \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt

FROM python:3.13-slim AS runtime
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 libsm6 libxext6 libxrender1 \
    protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY amber/ amber/
COPY eval_data/ eval_data/
COPY pytest.ini .

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5555/api/health'); r.raise_for_status()"

ENV AMBER_SOURCE=webcam
ENV AMBER_PORT=5555
ENV AMBER_MAVLINK_ADDRESS=udp://:14540
ENV AMBER_EDGE_WS=ws://localhost:9000
EXPOSE 5555

CMD ["python", "-m", "amber.dashboard"]
