# Build only C++ sources; local objects must never enter this build.
FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends g++ make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY core_engine/ core_engine/
COPY communication/ communication/
RUN make -C core_engine -j2 && make -C communication -j2

FROM python:3.12-slim-bookworm AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=10000
# Ingestion tools are available for an explicit, separate preparation session.
# Neither downloads nor ingestion run in the service's startup command.
RUN pip install --no-cache-dir numpy==2.2.6 requests==2.32.5
COPY --from=build /build/core_engine/eva_engine core_engine/eva_engine
COPY --from=build /build/communication/eva_bus communication/eva_bus
COPY prototipo/ prototipo/
COPY scripts/ scripts/
COPY data/processed/ data/processed/
EXPOSE 10000
CMD ["sh", "-c", "exec python3 prototipo/servidor.py --cloud --host 0.0.0.0 --port \"${PORT:-10000}\" --backend /app"]
