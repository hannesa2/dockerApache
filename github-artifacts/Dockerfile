FROM python:3.10-slim

WORKDIR /app

ARG APP_VERSION=unknown

COPY githubArtifactsPy/ /app/
COPY github-artifacts/prefix-support.patch /tmp/prefix-support.patch

RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir -r requirements.txt gunicorn==22.0.0 \
    && patch -d /app -p1 < /tmp/prefix-support.patch \
    && rm /tmp/prefix-support.patch \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 5000

CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:5000", "--access-logfile", "-", "--error-logfile", "-", "main:app"]
