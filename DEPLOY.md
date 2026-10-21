# Auto-deploy on push

Every push to `main` runs `stocky` on the server through a self-hosted GitHub
Actions runner. `.github/workflows/deploy.yml` is the whole pipeline; there is
no checkout, no third-party action, and no secret involved.

## Security warning (public repository)

GitHub recommends **not** attaching self-hosted runners to public repositories.
The mitigations used here:

- The workflow triggers only on `push` to `main` and manual dispatch.
- No `pull_request` or `pull_request_target` trigger. Never add one.
- No third-party actions, so nothing from the marketplace runs on the runner.
- The job is restricted to `jorgecanut/10kanalyzer` and `refs/heads/main`.
- Only users with write access can push to `main` or dispatch the workflow.

Anyone can still open a PR, but its code cannot reach the runner unless a
maintainer merges it. Review merged changes as you would any server change.

## 1. Install the runner

Use GitHub's current install commands: repository **Settings → Actions →
Runners → New self-hosted runner**. Run it as a dedicated user, not as your
login and not as root. `docker` group membership is effectively root-equivalent,
so keep that user limited to this machine's deploy duties.

```bash
sudo useradd -m -s /bin/bash ghrunner
sudo usermod -aG docker ghrunner          # only if stocky uses rootful Docker
sudo -iu ghrunner
mkdir -p ~/actions-runner && cd ~/actions-runner
# Download the tarball shown on the "New self-hosted runner" page, then:
tar xzf actions-runner-linux-x64-*.tar.gz
./config.sh \
  --url https://github.com/jorgecanut/10kanalyzer \
  --token <RUNNER_TOKEN_FROM_GITHUB_UI> \
  --labels stocky \
  --name stocky-01 \
  --unattended
sudo ./svc.sh install ghrunner
sudo ./svc.sh start
```

The `stocky` label is required: the workflow targets `[self-hosted, stocky]`.

Prefer an ephemeral runner (`--ephemeral`) restarted after each job if you want
to limit what a bad deploy can leave behind.

## 2. Requirements for `stocky`

- On `PATH` for the `ghrunner` user (the systemd service has a minimal `PATH`;
  check with `sudo -iu ghrunner which stocky`).
- Non-interactive and fail-fast, e.g.:

  ```bash
  #!/usr/bin/env bash
  set -euo pipefail
  cd /mnt/tanques/stocky/10kanalyzer
  git pull --ff-only
  docker compose build
  docker compose up -d --remove-orphans
  ```

- Any failure must exit non-zero, otherwise the workflow reports success.
- A deploy lock is optional: the workflow already serialises runs with
  `concurrency`, so two pushes queue rather than overlap.

## 3. Repository settings

- **Settings → Actions → General → Fork pull request workflows**: require
  approval for all outside collaborators. This is the default for public repos;
  do not relax it.
- Keep this the only workflow, and keep it free of `pull_request` triggers.

## 4. Verify

- Merge this change. The run stays **Queued** until the runner is online.
- Start the runner; the queued job should deploy.
- Later, use **Actions → Deploy to server → Run workflow** to test on demand.
