# Observability coverage and hard boundary

All metric labels are fixed allowlists. Project, item, version, file, principal,
filename, URL, token, physical path, raw IP, and request path are never labels.
Prometheus and cAdvisor have no host-published port and use only the management
network. Metrics failures are caught and do not block business requests.

| Required signal | Source | Current implementation | Hard state |
| --- | --- | --- | --- |
| API volume, status, concurrency, p50/p95/p99 | ASGI wrapper and Prometheus histogram quantiles | Fixed `route_family`, method and status; bounded buckets | PASS_TESTED: backend target was scraped successfully in the isolated Compose stack |
| Project aggregate versus legacy detail/item/version/issue endpoints | ASGI route classifier | Each family is fixed; identifiers are discarded before label creation | PASS_STATIC |
| Workspace refresh/poll load | ASGI route classifier | Counts project aggregate/detail/items GET refreshes by family and status | PASS_STATIC; client intent is refresh, not an inferred user identity |
| FastAPI stream authorization and Range request presence | ASGI wrapper | Counts authorization status and whether a Range header was present; never records the Range value | PASS_STATIC |
| NGINX-served Range TTFB, duration, bytes, 206, failure and cancellation | NGINX OSS | Bounded access log has NGINX-generated request id, method, status, bytes and duration; no IP/path/query/header. It is not parsed into Prometheus | BLOCKED: NGINX OSS has no native Prometheus request metrics, a custom log parser is forbidden, and an additional exporter is outside the frozen component list |
| Upload init/part/status/complete/abort count, outcome, timeout and cancellation | ASGI wrapper | Fixed operation and outcome labels | PASS_STATIC |
| Upload rejection reason | Exact admission branches call `observe_upload_rejection` | Fixed reasons: global/principal session, global/principal reservation, cleanup pending, low watermark, storage unavailable, too large, invalid, other; current service calls the confirmed quota/low-water/storage branches directly | PASS_STATIC; cleanup-pending remains a database gauge because it is not a distinct current rejection branch |
| Active sessions, global reservation, maximum single-principal reservation, cleanup pending, package reservation, free bytes and low watermark | Bounded database collector and managed-filesystem stat | No principal or path label; configured limits exported separately | PASS_TESTED: isolated PostgreSQL and staging collectors both reported success |
| Package queue, attempts, failures, reserved/storage bytes and staging | Bounded database collector and no-follow bounded staging scan | Fixed status/failure labels; staging scan stops at 10,000 entries | PASS_STATIC |
| Package execution duration and per-attempt outcome | Package worker calls `observe_package_task` around the actual attempt | Fixed outcome/failure labels and bounded duration buckets; best-effort port 9101 is exposed only on the internal management network and has a dedicated Prometheus scrape job | PASS_TESTED: target and metric families were scraped in the isolated stack; no package attempt existed to create a non-empty result series |
| Media backlog, attempts, failures and staging | Bounded database collector and no-follow bounded staging scan | Fixed kind/status/failure labels | PASS_STATIC |
| Media probe/faststart/thumbnail execution duration and per-attempt outcome | Media worker calls `observe_media_task` around the claimed task | Fixed kind/outcome/failure labels and bounded duration buckets; best-effort port 9102 is exposed only on the internal management network and has a dedicated Prometheus scrape job | PASS_TESTED: target and metric families were scraped in the isolated stack; no media attempt existed to create a non-empty result series |
| Backend/package/media/PostgreSQL CPU, memory, restart, filesystem and OOM pressure | cAdvisor | Default-disabled approval profile, `privileged:false`, all capabilities dropped, read-only mounts/device, management network only | BLOCKED until a real approved Linux host proves required metrics work without elevated privileges |
| High-call/high-total-time/high-I/O/temp SQL | pg_stat_statements | Preloaded with query IDs; runtime gets only a hardened aggregate security-definer function and never SQL text or `pg_read_all_stats` | PASS_TESTED: isolated runtime role read the aggregate, was denied SQL text, and was not a `pg_read_all_stats` member |

Prometheus graphs, Compose parsing, or a healthy container cannot upgrade a
blocked row. The production and commercial release gates remain blocked until
the corresponding real environment and legal checks are complete.
