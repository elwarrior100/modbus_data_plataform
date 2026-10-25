FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 sim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# pymodbus vai só para poder verificar o RS-232 de dentro do contentor com um cliente independente
RUN pip install --no-cache-dir . pymodbus

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER sim
EXPOSE 5020 8080
ENTRYPOINT ["/entrypoint.sh"]
