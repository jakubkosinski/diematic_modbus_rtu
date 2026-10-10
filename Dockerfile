FROM python:3.12-slim AS build
WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DIEMATIC_HEARTBEAT_FILE=/tmp/diematic-heartbeat
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin diematic
COPY --from=build /dist/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels
USER diematic
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["diematic", "healthcheck", "--max-age", "60"]
ENTRYPOINT ["diematic"]
CMD ["run"]
