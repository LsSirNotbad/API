FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl default-mysql-client iputils-ping tzdata \
    && rm -rf /var/lib/apt/lists/*

# pip 源可在构建时通过 --build-arg PIP_INDEX_URL=... 覆盖
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_DEFAULT_TIMEOUT=60
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/backups /app/exports /app/data

EXPOSE 443

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -k -f https://127.0.0.1/health || exit 1

CMD ["python", "main.py"]
