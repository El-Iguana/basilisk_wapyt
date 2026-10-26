#!/usr/bin/env bash
# A throwaway CockroachDB for the tests: one insecure node, user root, no
# password, SQL on 127.0.0.1:56257 (the DB Console on :58080).
set -euo pipefail
name=basilisk-crdb
if podman container exists "$name"; then
  podman start "$name" >/dev/null
else
  podman run -d --name "$name" -p 127.0.0.1:56257:26257 -p 127.0.0.1:58080:8080 \
    docker.io/cockroachdb/cockroach:latest start-single-node --insecure >/dev/null
fi
echo "CockroachDB: $name on 127.0.0.1:56257 (user root, no password)"
