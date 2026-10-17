# syntax=docker/dockerfile:1
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUANT_LAB_HOME=/data \
    HOME=/data \
    MPLCONFIGDIR=/tmp/matplotlib \
    QUANT_LAB_CONTAINER=1
RUN groupadd --gid 10001 quantlab \
    && useradd --uid 10001 --gid quantlab --no-create-home quantlab \
    && mkdir /data && chown quantlab:quantlab /data
COPY requirements-docker.lock /opt/requirements-docker.lock
RUN python -m pip install --no-cache-dir -c /opt/requirements-docker.lock python-dotenv tzdata
WORKDIR /data
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "quant_lab"]

FROM runtime AS paper-deps
RUN python -m pip install --no-cache-dir -r /opt/requirements-docker.lock

FROM runtime AS demo
COPY --from=builder /wheels/ /wheels/
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels
USER quantlab
EXPOSE 8767
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8767/api/automation',timeout=3)); assert r['status']=='DEMO'"
CMD ["demo", "--port", "8767"]

FROM paper-deps AS paper
COPY --from=builder /wheels/ /wheels/
RUN python -m pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels
USER quantlab
EXPOSE 8766
HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=3 \
    CMD python -m quant_lab.services.healthcheck
CMD ["container", "--execute-paper"]
