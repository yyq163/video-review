FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS ffmpeg-apv-builder

ARG FFMPEG_APV_VERSION=9.0.1
ARG FFMPEG_APV_SOURCE_SHA256=cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635
ARG FFMPEG_APV_SIGNATURE_SHA256=b613a00005232a1245ace7080088781ac23a916119d3e5b0d6c042368eee0177
ARG FFMPEG_APV_SIGNING_KEY_SHA256=397b3becedcd5a98769967ff1ff8501ddc89f8368b8f766e4701377d7dbaabe5
ARG FFMPEG_APV_SIGNING_KEY_FINGERPRINT=FCF986EA15E6E293A5644F10B4322F04D67658D8
ARG FFMPEG_APV_LICENSE_SHA256=2e1d16c72fd74e12063776371da757322f8b77589386532f4fd8634bde7de1af
ARG FFMPEG_APV_COPYING_SHA256=246041b6ecf9bc32d718a62c57877c78b5eb397b6467e74ed7ae2626ab189c30
ARG FFMPEG_APV_BUILD_JOBS=4

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        gnupg \
        nasm \
        pkg-config \
        xz-utils \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src

RUN curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 \
        "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_APV_VERSION}.tar.xz" \
        --output ffmpeg.tar.xz \
    && curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 \
        "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_APV_VERSION}.tar.xz.asc" \
        --output ffmpeg.tar.xz.asc \
    && curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 \
        https://ffmpeg.org/ffmpeg-devel.asc \
        --output ffmpeg-devel.asc \
    && printf '%s  %s\n' \
        "${FFMPEG_APV_SOURCE_SHA256}" ffmpeg.tar.xz \
        "${FFMPEG_APV_SIGNATURE_SHA256}" ffmpeg.tar.xz.asc \
        "${FFMPEG_APV_SIGNING_KEY_SHA256}" ffmpeg-devel.asc \
        | sha256sum --check --strict - \
    && export GNUPGHOME="$(mktemp -d)" \
    && chmod 0700 "${GNUPGHOME}" \
    && gpg --batch --import ffmpeg-devel.asc \
    && test "$(gpg --batch --with-colons --fingerprint "${FFMPEG_APV_SIGNING_KEY_FINGERPRINT}" \
        | awk -F: '$1 == "fpr" {print $10; exit}')" = "${FFMPEG_APV_SIGNING_KEY_FINGERPRINT}" \
    && gpg --batch --verify ffmpeg.tar.xz.asc ffmpeg.tar.xz \
    && rm -rf "${GNUPGHOME}" ffmpeg-devel.asc ffmpeg.tar.xz.asc \
    && tar --extract --file ffmpeg.tar.xz \
    && rm ffmpeg.tar.xz

WORKDIR /usr/src/ffmpeg-9.0.1

RUN ./configure \
        --prefix=/opt/ffmpeg-apv \
        --disable-debug \
        --disable-doc \
        --disable-ffplay \
        --disable-network \
        --disable-autodetect \
        --disable-avdevice \
        --disable-everything \
        --disable-sdl2 \
        --disable-xlib \
        --disable-cuda-llvm \
        --disable-cuvid \
        --disable-ffnvcodec \
        --disable-nvdec \
        --disable-nvenc \
        --disable-vulkan \
        --enable-static \
        --disable-shared \
        --enable-zlib \
        --enable-decoder=apv \
        --enable-encoder=mjpeg \
        --enable-encoder=rawvideo \
        --enable-demuxer=mov \
        --enable-muxer=mp4 \
        --enable-muxer=nut \
        --enable-muxer=image2 \
        --enable-muxer=rawvideo \
        --enable-parser=apv \
        --enable-parser=h264 \
        --enable-parser=hevc \
        --enable-parser=aac \
        --enable-bsf=apv_metadata \
        --enable-protocol=fd \
        --enable-protocol=file \
        --enable-protocol=pipe \
        --enable-filter=select \
        --enable-filter=scale \
        --enable-filter=format \
    && make -j"${FFMPEG_APV_BUILD_JOBS}" \
    && make install \
    && printf '%s  %s\n' \
        "${FFMPEG_APV_LICENSE_SHA256}" LICENSE.md \
        "${FFMPEG_APV_COPYING_SHA256}" COPYING.LGPLv2.1 \
        | sha256sum --check --strict - \
    && install -D -m 0644 LICENSE.md /opt/ffmpeg-apv/share/licenses/FFmpeg-LICENSE.md \
    && install -D -m 0644 COPYING.LGPLv2.1 /opt/ffmpeg-apv/share/licenses/FFmpeg-COPYING.LGPLv2.1 \
    && /opt/ffmpeg-apv/bin/ffmpeg -hide_banner -decoders 2>&1 | grep -Eq '[[:space:]]apv[[:space:]]' \
    && /opt/ffmpeg-apv/bin/ffprobe -version 2>&1 | grep -F "ffprobe version ${FFMPEG_APV_VERSION}" \
    && /opt/ffmpeg-apv/bin/ffmpeg -buildconf > /opt/ffmpeg-apv/share/ffmpeg-apv-buildconf.txt 2>&1 \
    && ! grep -Eq -- '--enable-(gpl|nonfree)' /opt/ffmpeg-apv/share/ffmpeg-apv-buildconf.txt \
    && printf '%s\n' "${FFMPEG_APV_SOURCE_SHA256}" > /opt/ffmpeg-apv/share/ffmpeg-apv-source-sha256.txt \
    && { ldd /opt/ffmpeg-apv/bin/ffmpeg; ldd /opt/ffmpeg-apv/bin/ffprobe; } \
        | sort -u > /opt/ffmpeg-apv/share/ffmpeg-apv-dynamic-libraries.txt

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
    && ffmpeg -hide_banner -filters > /tmp/ffmpeg-filters.txt 2>&1 \
    && for filter_name in blackframe blackdetect signalstats scdet silencedetect scale metadata split nullsink; do \
        grep -Eq "[[:space:]]${filter_name}[[:space:]]" /tmp/ffmpeg-filters.txt; \
    done \
    && rm /tmp/ffmpeg-filters.txt \
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

COPY --from=ffmpeg-apv-builder /opt/ffmpeg-apv /opt/ffmpeg-apv

RUN /opt/ffmpeg-apv/bin/ffmpeg -hide_banner -decoders 2>&1 | grep -Eq '[[:space:]]apv[[:space:]]' \
    && /opt/ffmpeg-apv/bin/ffprobe -version 2>&1 | grep -F 'ffprobe version 9.0.1' \
    && ! /opt/ffmpeg-apv/bin/ffmpeg -buildconf 2>&1 | grep -Eq -- '--enable-(gpl|nonfree)'

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
