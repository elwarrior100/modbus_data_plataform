FROM python:3.12-slim

RUN useradd --create-home --uid 10001 ingest && mkdir /data && chown ingest /data
COPY . /src
RUN pip install --no-cache-dir /src

USER ingest
VOLUME /data
ENTRYPOINT ["ingest"]
