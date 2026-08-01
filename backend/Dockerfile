FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280

ARG APP_UID=10001
ARG APP_GID=10001
ARG FFMPEG_PACKAGE_VERSION=7:7.1.5-0+deb13u1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg="${FFMPEG_PACKAGE_VERSION}" \
        gosu \
    && install -d -m 0755 /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && printf '%s  %s\n' \
        '0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76' \
        '/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc' \
        | sha256sum --check --strict - \
    && . /etc/os-release \
    && printf '%s\n' \
        "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5=18.4-1.pgdg13+1 \
        postgresql-client-common=291.pgdg13+1 \
        postgresql-client-16=16.14-1.pgdg13+1 \
    && test "$(pg_dump --version | awk '{print $3}' | cut -d. -f1)" = 16 \
    && test "$(pg_restore --version | awk '{print $3}' | cut -d. -f1)" = 16 \
    && test "$(dpkg-query --show --showformat='${Version}' ffmpeg)" = "${FFMPEG_PACKAGE_VERSION}" \
    && ffprobe -version >/dev/null \
    && ! ffmpeg -buildconf 2>&1 | grep -F -- '--enable-nonfree' \
    && install -d -m 0755 /usr/share/fj-build \
    && ffmpeg -version > /usr/share/fj-build/ffmpeg-version.txt 2>&1 \
    && ffmpeg -buildconf > /usr/share/fj-build/ffmpeg-buildconf.txt 2>&1 \
    && ffprobe -version > /usr/share/fj-build/ffprobe-version.txt 2>&1 \
    && dpkg-query --show --showformat='${Package}\t${Version}\t${Architecture}\n' \
        ffmpeg 'libavcodec*' 'libavdevice*' 'libavfilter*' 'libavformat*' \
        'libavutil*' 'libpostproc*' 'libswresample*' 'libswscale*' \
        | sort -u > /usr/share/fj-build/ffmpeg-debian-packages.tsv \
    && { ldd /usr/bin/ffmpeg; ldd /usr/bin/ffprobe; } \
        | sort -u > /usr/share/fj-build/ffmpeg-dynamic-libraries.txt \
    && apt-get purge -y --auto-remove curl \
    && rm -f /etc/apt/sources.list.d/pgdg.list /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY contracts contracts

RUN groupadd --system --gid "${APP_GID}" app \
    && useradd --system --uid "${APP_UID}" --gid app --home-dir /app --no-create-home app \
    && mkdir -p /app/backend/.data /data /var/lib/fj-runtime-state \
    && chown root:root /var/lib/fj-runtime-state \
    && chmod 0755 /var/lib/fj-runtime-state \
    && chown -R app:app /app /data

ENTRYPOINT ["sh", "backend/docker-entrypoint.sh"]

EXPOSE 8000

CMD ["sh", "-c", "python -c 'from backend.app.main import database_readiness; database_readiness()' && exec uvicorn backend.app.main:app --host 0.0.0.0 --port 8000"]
