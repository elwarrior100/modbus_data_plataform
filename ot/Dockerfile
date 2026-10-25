# Imagem "ot-edge": par série virtual (socat) + simulador Modbus + gateway OPC UA.
# O par pty só existe dentro do contentor que o cria; por isso os três correm juntos,
# como uma caixa de campo com linha série e gateway lado a lado.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 ot

COPY simulator /src/simulator
COPY gateway /src/gateway
RUN pip install --no-cache-dir /src/simulator /src/gateway

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER ot
EXPOSE 5020 8080 4840
ENTRYPOINT ["/entrypoint.sh"]
