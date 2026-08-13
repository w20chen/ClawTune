# Multi-arch base: python:3.12-slim supports linux/amd64 and linux/arm64 (Kunpeng).
# Build with: docker build --platform linux/arm64 -t clawtune .
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

EXPOSE 8765

CMD ["python", "-m", "clawtune_sidecar.main", "--host", "0.0.0.0", "--port", "8765"]
