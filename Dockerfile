FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY gscore_qq ./gscore_qq
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 adapter
COPY --from=builder /install /usr/local
USER adapter
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "gscore_qq"]
