# Third-party notices

This file records the direct infrastructure additions for the repair-list
candidate. It is not a commercial-release approval. The exact customer
distribution bundle must include the generated SBOM, license index, candidate
image digests, and the runtime build records from `/usr/share/fj-build/`.

## NGINX Open Source

NGINX Open Source 1.31.3 is distributed under the 2-clause BSD license. The
locked Alpine image also contains independently licensed zlib, PCRE2, OpenSSL,
musl, and Alpine packages. Their exact versions and notices are properties of
the immutable image digest in `component-lock.json`; an image-layer SBOM and
license bundle must be attached before customer distribution.

## PostgreSQL and pg_stat_statements

PostgreSQL 16 and its bundled `pg_stat_statements` extension are distributed
under the PostgreSQL License. The runtime application role is not granted
`pg_read_all_stats` and cannot select SQL text. It can execute only the bounded
aggregate function installed by database bootstrap.

## Prometheus, prometheus-client, and cAdvisor

Prometheus 3.13.0, prometheus-client 0.25.0, and cAdvisor 0.57.0 are Apache
License 2.0 components. The prometheus-client NOTICE is preserved in the
generated license bundle. Prometheus's LICENSE and NOTICE are copied byte for
byte from the immutable upstream `v3.13.0` commit recorded in
`upstream-component-materials.json`. cAdvisor's LICENSE is likewise copied from
its immutable upstream `v0.57.0` commit. That cAdvisor source tree has no root
NOTICE file; the immutable tree URL, expected NOTICE URL, check date, and
absence conclusion are recorded in the manifest, and no project-authored
substitute has been created. No local modifications are made to these upstream
components; project configuration and integration code are separate works.

## FFmpeg and codec libraries

FFmpeg and its linked libraries retain their upstream and Debian authorship,
licenses, and trademarks. They are not project-owned technology. The backend
image build pins Debian package `7:7.1.5-0+deb13u1`, rejects
`--enable-nonfree`, and records `ffmpeg -version`, `ffmpeg -buildconf`,
`ffprobe -version`, Debian package versions, and dynamic libraries.

The actual GPL/LGPL obligations are determined by those candidate-image build
records and linked libraries. If `--enable-gpl` or GPL libraries are present,
the distribution must be handled as GPL; it must not be described as an LGPL
build. Target-region patent review for H.264, H.265, AAC, and any final codec
combination is still incomplete, so commercial release remains blocked.

## Generated dependency notices

`generate_compliance_artifacts.py` creates separate SaaS and customer
CycloneDX SBOMs from the immutable container lock, Python environment dependency
closure, and every resolved package in `package-lock.json`. It also creates a
deduplicated license-text bundle and index from installed Python and npm
artifacts. Missing installed license files are written to
`unresolved-license-texts.json` and keep commercial release blocked; they are
never silently classified.
