# Deploy without GitHub

Deploys go straight from the laptop to the TrueNAS box over the VPN. GitHub is
an optional backup remote and plays no part in deployment.

```
laptop ──git push nas main──► /mnt/tanques/stocky/10kanalyzer.git (bare)
                                     │ post-receive hook
                                     ▼
                            /mnt/tanques/stocky/10kanalyzer
                            git reset --hard origin/main
                            docker compose up -d --build
                            curl /healthz
```

## One-time setup

1. Install the laptop key on the server (one password prompt):

   ```bash
   ssh-copy-id -i ~/.ssh/stocky.pub truenas_admin@canut.xyz
   ```

   `~/.ssh/config` already maps `nas` to `canut.xyz` with that key.

2. Create the bare repo, repoint the server checkout and install the hook:

   ```bash
   ssh nas 'bash -s' < scripts/nas-setup.sh
   ```

   The script prints a warning if the deploy user cannot reach Docker. Fix it
   with `sudo usermod -aG docker truenas_admin` and a fresh login, or make
   `sudo -n docker` work; the hook tries both.

3. Push:

   ```bash
   git push nas main      # or: git deploy
   ```

   The push blocks until the image is rebuilt and `/healthz` answers. Full
   output also lands in `/mnt/tanques/stocky/deploy.log`.

## Rules

- The server checkout is a **deploy target**: never edit files there. The hook
  runs `git reset --hard`, so local edits are discarded. Runtime state lives in
  bind mounts (`financial_graphs`, `edgar_cache`, `edgar_data`) and `.env`,
  all untracked.
- Only `main` triggers a deploy. Other branches can be pushed without a rebuild.
- GitHub stays as a backup: `git push origin main` still works, but it no longer
  deploys anything. The self-hosted runner is no longer needed; stop and remove
  it at leisure.
- To redeploy the current commit (e.g. after changing `.env`):

  ```bash
  ssh nas 'cd /mnt/tanques/stocky/10kanalyzer && docker compose up -d --build'
  ```

- Verify with `/healthz`: `features` and the `sha1` of `edgar_dashboard.py` /
  `charts.py` must match the pushed commit (`sha1sum` in the repo root).

## Troubleshooting

- **Push rejects the hook** — read the tail of `/mnt/tanques/stocky/deploy.log`;
  the push fails when the build or health check fails.
- **`no docker access`** — the deploy user is not in the `docker` group and
  passwordless sudo is unavailable.
- **App runs old code** — compare the `sha1` values from `/healthz` with
  `sha1sum` of the checkout; a mismatched hash means the container was not
  rebuilt.
