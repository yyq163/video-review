# Distribution matrix and release boundary

| Form | Included technical inventory | Required before release | Current state |
| --- | --- | --- | --- |
| SaaS deployment | Backend image, PostgreSQL, NGINX, Prometheus, optional approval-gated cAdvisor, Python runtime dependencies, built frontend dependency provenance | Candidate image digests, runtime FFmpeg records, production config/backup/rollback approval, target-region codec review if the service encodes or redistributes covered codecs | BLOCKED |
| Customer Docker images | Backend, PostgreSQL, NGINX, and any separately delivered observability images; Python and frontend dependency provenance | Per-platform image SBOMs and digests, full image-layer license texts/notices including Alpine/zlib/PCRE2/OpenSSL, FFmpeg GPL/LGPL classification and corresponding source/offer obligations, target-region patent review | BLOCKED |
| Customer installer or appliance | Everything in customer Docker images plus host OS, installer, firmware, drivers, bundled fonts/media, and hardware notices | Appliance-specific SBOM, reproducible installer digest, written notices/source-offer delivery, trademark review, target-region patent review | BLOCKED |

cAdvisor is not enabled by default. Enabling its host mounts or device access in
production is a separate privileged-operation approval even when the service is
present in a customer inventory.

The committed SBOMs describe the source candidate. A customer-release SBOM is
not final until it is regenerated against the immutable candidate images and
the actual installer/appliance payload.
