# Third-party notices

This file records the direct infrastructure additions for the current
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

The 2026-08-21 dual-runtime candidate image
`sha256:290f16a94d21dcf87e666f2584775276ef21a2259d9acb45df6e61871bb9e331`
contains two independently inventoried FFmpeg runtimes. Debian FFmpeg
`7:7.1.5-0+deb13u1` reports `--enable-gpl`, no `--enable-nonfree`, shared
libraries, and FFmpeg's GPL version 2 or later notice. Its exact
`/usr/share/common-licenses/GPL-2` and `/usr/share/doc/ffmpeg/copyright` files
are preserved byte-for-byte in the generated license index under
`locked:deb:ffmpeg`.

The isolated `/opt/ffmpeg-apv` runtime is built from the signed official FFmpeg
9.0.1 source release for CPU-only APV decoding and NUT muxing. Its source archive and detached
signature are SHA-256 locked, the release-key fingerprint is locked, and the
installed `ffmpeg` and `ffprobe` binaries are hashed for the verified
architecture. Its build configuration disables network access, CUDA/NVIDIA,
Vulkan, GPL, nonfree, `liboapv`, and H.264 decoding capabilities. The upstream FFmpeg license
summary and LGPL-2.1 text are preserved byte-for-byte in the generated license
index under `locked:source:ffmpeg-apv`.

The candidate application account is locked to `app` with numeric identity
`3001:3003`, matching the production NFS ownership contract. Both SBOMs record
the UID and GID. Candidate verification resolves `app` through the image's
passwd/group databases and runs the runtime checks under that exact numeric
identity; a digest or identity mismatch fails closed.

Both the SaaS and customer-container SBOMs list both runtimes because the same
backend image physically contains them. This inventory is not customer
redistribution approval. The GPL corresponding-source procedure, APV arm64
verification, customer redistribution, target-region patent review for H.264,
H.265, AAC, APV, and the final codec combination remain blocked; commercial
release therefore remains blocked.

## Generated dependency notices

`generate_compliance_artifacts.py` creates separate SaaS and customer
CycloneDX SBOMs from the immutable container lock, Python environment dependency
closure, and every resolved package in `package-lock.json`. It also creates a
deduplicated license-text bundle and index from installed Python and npm
artifacts. Missing installed license files are written to
`unresolved-license-texts.json` and keep commercial release blocked; they are
never silently classified.
