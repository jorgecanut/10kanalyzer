#!/usr/bin/env bash
# One-time setup for direct deploys from the laptop over the VPN.
#
# Run ON THE TRUENAS BOX as the user that will receive pushes:
#
#   ssh nas 'bash -s' < scripts/nas-setup.sh
#
# Creates a bare repo, points the existing checkout at it, and installs a
# post-receive hook that rebuilds the container on every push to main.
set -euo pipefail

BARE=/mnt/tanques/stocky/10kanalyzer.git
WORK=/mnt/tanques/stocky/10kanalyzer
LOG=/mnt/tanques/stocky/deploy.log

[ -d /mnt/tanques/stocky ] || { echo "[!] /mnt/tanques/stocky missing" >&2; exit 1; }
[ -d "$WORK/.git" ] || { echo "[!] $WORK is not a git checkout" >&2; exit 1; }

echo "== bare repo: $BARE"
if [ ! -d "$BARE" ]; then
  git init --bare "$BARE" >/dev/null
  git -C "$BARE" symbolic-ref HEAD refs/heads/main
fi
git -C "$BARE" config receive.denyDeleteCurrent ignore

echo "== checkout: $WORK"
git -C "$WORK" remote remove nas 2>/dev/null || true
git -C "$WORK" remote set-url origin "$BARE"
git -C "$WORK" fetch --prune origin || true

echo "== hook: $BARE/hooks/post-receive"
cat >"$BARE/hooks/post-receive" <<HOOK
#!/usr/bin/env bash
set -euo pipefail

BARE=$BARE
WORK=$WORK
LOG=$LOG

deploy=0
while read -r old new ref; do
  if [ "\$ref" = "refs/heads/main" ]; then deploy=1; fi
done
if [ "\$deploy" != 1 ]; then
  echo "post-receive: only main deploys; nothing to do"
  exit 0
fi

exec > >(tee -a "\$LOG") 2>&1
echo
echo "=== deploy start \$(date -Is) ==="

cd "\$WORK"
git fetch --prune origin
git checkout -f -B main origin/main
git reset --hard origin/main

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  echo "[!] no docker access for \$(id -un); run: sudo usermod -aG docker \$(id -un)"
  exit 1
fi

"\${DOCKER[@]}" compose up -d --build --remove-orphans
"\${DOCKER[@]}" compose ps

for _ in \$(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8053/healthz >/dev/null 2>&1; then
    echo "=== deploy ok \$(date -Is) ==="
    exit 0
  fi
  sleep 2
done

echo "[!] /healthz not ready after 60s"
exit 1
HOOK
chmod +x "$BARE/hooks/post-receive"

echo "== checks"
[ -f "$WORK/.env" ] || echo "[!] $WORK/.env missing (data falls back to ./data, ./edgar_cache, ./edgar_data)"
command -v curl >/dev/null || echo "[!] curl missing; the hook cannot verify /healthz"
if docker info >/dev/null 2>&1; then
  echo "docker: direct access ok"
elif sudo -n docker info >/dev/null 2>&1; then
  echo "docker: passwordless sudo ok"
else
  echo "[!] docker needs a group/sudo fix before the first push will deploy:"
  echo "    sudo usermod -aG docker $(id -un)   # then log out and back in"
fi
if [ -x /usr/local/bin/stocky ]; then
  echo "note: stocky now pulls from $BARE, so GitHub pushes deploy the NAS commit, not the GitHub one"
fi

echo
echo "Done. From the laptop:  git push nas main"
