# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim AS demo
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QUANT_LAB_HOME=/data \
    QUANT_LAB_CONTAINER=1
RUN groupadd --gid 10001 quantlab \
    && useradd --uid 10001 --gid quantlab --no-create-home quantlab \
    && mkdir /data && chown quantlab:quantlab /data
COPY --from=builder /wheels/ /wheels/
RUN python -m pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER quantlab
WORKDIR /data
EXPOSE 8767
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8767/api/automation',timeout=3)); assert r['status']=='DEMO'"
ENTRYPOINT ["python", "-m", "quant_lab"]
CMD ["demo", "--port", "8767"]
