# Caddy to internal NGINX handoff

Caddy remains the only browser-facing TLS/API entry. The internal NGINX port is
bound to host loopback and rejects every proxied API request unless Caddy adds
the `X-FCR-Edge-Handoff` value from the approved secret source. Caddy must
remove any client-supplied value before adding its own.

The example is not a production deployment authorization. Before changing the
existing Caddy service, record its exact identity and current config, provision
the secret through its service manager (not a command-line argument), validate
the candidate config, and prepare rollback to the prior config.

FastAPI remains the authorization authority. For an authorized playback asset
it may return `X-Accel-Redirect: /_protected_media/<opaque_file_id>`. NGINX's
matching location is `internal`, maps only a bounded opaque identifier into the
fixed managed files directory, and is not reachable directly. A missing or
invalid edge handoff fails closed; it must never trigger a public-file fallback.
