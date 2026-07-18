FROM python:3.12-slim

# bluez provides bluetoothctl, which printer.py uses to pin PreferredBearer=le.
# It talks to the HOST bluetoothd via the mounted D-Bus socket; no daemon runs
# in the container. fonts-dejavu-core is needed for text rendering.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bluez fonts-dejavu-core curl libzbar0 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py rendering.yaml ./
COPY static ./static

ENV MXW01_ADDRESS=48:0F:57:49:DB:3B \
    MXW01_INTENSITY=0x5D \
    PORT=8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT}"]
