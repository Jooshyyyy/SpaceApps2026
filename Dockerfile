# ARES · Mars EVA Nav en la nube: página + base de control + motor (eva_engine) + bus (eva_bus).
# El reloj sigue por Bluetooth con la laptop: allí corre prototipo/puente_reloj.py.
# Variables: OPERADOR_TOKEN y RELOJ_TOKEN (secretos, ≥20 letras/números). PORT lo pone la plataforma.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ make \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY core_engine core_engine
COPY communication communication
COPY data/processed data/processed
COPY prototipo prototipo

# Motor y bus del equipo compilados para Linux de la imagen (sin cmake ni librerías externas)
RUN make -C core_engine clean all \
 && make -C communication clean all \
 && for f in aoi.json cost.bin elevation.bin slope.bin radiation.bin thermal_inertia.bin; do \
      test -s "data/processed/$f" || { echo "Falta data/processed/$f (capas de la zona)"; exit 1; }; done

RUN useradd --create-home ares && chown -R ares /app/prototipo
USER ares
ENV PORT=8080
EXPOSE 8080
CMD ["python3", "-I", "prototipo/servidor.py", "--publico", "--sin-hardware", "--host", "0.0.0.0"]
