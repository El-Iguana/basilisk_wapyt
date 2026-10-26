#!/usr/bin/env bash
# A throwaway SSH server for the SSH tunnel tests: OpenSSH on 127.0.0.1:52222
# (host network, so it reaches the test databases on 127.0.0.1), user
# "tunnel" with password Tunn3l-pass and the key in data/ssh-test/id_ed25519,
# which this script makes once (data/ is git-ignored).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEYS="$ROOT/data/ssh-test"
mkdir -p "$KEYS"
[ -f "$KEYS/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C basilisk-test -f "$KEYS/id_ed25519"
# The same key with a passphrase, for the passphrase path.
[ -f "$KEYS/id_ed25519_locked" ] || {
  cp "$KEYS/id_ed25519" "$KEYS/id_ed25519_locked"
  ssh-keygen -q -p -N "sesame" -f "$KEYS/id_ed25519_locked" >/dev/null
}
PUB="$(cat "$KEYS/id_ed25519.pub")"
podman rm -f basilisk-ssh >/dev/null 2>&1 || true
podman run -d --name basilisk-ssh --network host docker.io/library/alpine:3.20 sh -c "
  apk add --no-cache openssh >/dev/null &&
  ssh-keygen -A >/dev/null &&
  adduser -D tunnel && echo 'tunnel:Tunn3l-pass' | chpasswd &&
  mkdir -p /home/tunnel/.ssh && echo '$PUB' > /home/tunnel/.ssh/authorized_keys &&
  chown -R tunnel /home/tunnel/.ssh && chmod 700 /home/tunnel/.ssh &&
  chmod 600 /home/tunnel/.ssh/authorized_keys &&
  exec /usr/sbin/sshd -D -e -p 52222 -o ListenAddress=127.0.0.1 \
    -o PasswordAuthentication=yes -o AllowTcpForwarding=yes -o PermitTTY=no" >/dev/null
for _ in $(seq 1 60); do
  if podman logs basilisk-ssh 2>&1 | grep -q "Server listening"; then
    echo "basilisk-ssh on 127.0.0.1:52222 (tunnel / Tunn3l-pass, key $KEYS/id_ed25519)"
    exit 0
  fi
  sleep 1
done
echo "sshd did not start:" >&2; podman logs basilisk-ssh >&2; exit 1
