FROM postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777

# Docker Desktop bind mounts can be non-executable while the upstream
# entrypoint briefly observes an executable bit during first initialization.
# Copy the script into the image as data so docker-entrypoint.sh sources it.
COPY --chmod=0644 ops/postgres/init/10-create-app-databases.sh /docker-entrypoint-initdb.d/10-create-app-databases.sh
COPY --chmod=0755 ops/postgres/postgres-entrypoint.sh /usr/local/bin/fcr-postgres-entrypoint

ENTRYPOINT ["fcr-postgres-entrypoint"]
CMD ["postgres"]
