# Running the 10-K Dashboard on Windows 11

This guide starts from a **clean Windows 11 install** and gets you to a working
dashboard at `http://localhost:8000`. Everything runs locally on your PC —
nothing is uploaded anywhere. You only need Docker; you do not need Python, Git,
or any developer tools.

Total time: ~15 minutes (mostly downloads). Disk: ~5 GB free.

---

## Quick start (already have Docker?)

```powershell
# in the project folder
Copy-Item .env.example .env
notepad .env                 # set EDGAR_IDENTITY to your name + email
docker compose up -d --build
```

Then open <http://localhost:8000>. If Docker isn't installed yet, keep reading.

---

## Step 1 — Install WSL2

Docker Desktop on Windows needs WSL2 (a small Linux layer).

1. Right-click **Start → Terminal (Admin)** (or **Windows Terminal (Admin)**).
2. Run:

   ```powershell
   wsl --install
   ```

3. **Restart** the PC when asked.
4. After the restart, a Linux window may open and ask for a **username** and
   **password** (any values — you will rarely use them). If it doesn't appear,
   that's fine.

> If `wsl --install` says it isn't recognized, run **Windows Update** first,
> then try again. If it complains about virtualization, enable **Intel VT-x /
> AMD-V** (SVM) in your BIOS — on most PCs it's already on. You can check via
> Task Manager → Performance → CPU → "Virtualization: Enabled".

---

## Step 2 — Install Docker Desktop

1. Download **Docker Desktop for Windows**:
   <https://www.docker.com/products/docker-desktop/>
2. Run `Docker Desktop Installer.exe`.
   - Keep **"Use WSL 2 instead of Hyper-V"** ticked.
   - Finish the installer and **restart** if it asks.
3. Start **Docker Desktop** from the Start menu.
   - Accept the service agreement.
   - You can **skip signing in** (click "Continue without signing in").
   - Wait until the whale icon in the system tray stops animating — the engine
     is ready when the icon is steady.
4. Allow it through the Windows Firewall prompt if one appears.

> **Licensing:** Docker Desktop is free for personal use, students, and small
> businesses (<250 employees and <$10M revenue). That covers personal use.

---

## Step 3 — Check Docker works

Open **Terminal** (no admin needed now) and run:

```powershell
docker --version
docker compose version
docker run --rm hello-world
```

You should see versions printed and a "Hello from Docker!" message.

---

## Step 4 — Get the project

Pick whichever is easiest:

- **Download the ZIP** you were given and extract it somewhere simple like
  `C:\dev\edgar-dashboard` (avoid very deep folders — Windows has a path-length
  limit).
- **Or, with Git installed:**

  ```powershell
  cd C:\dev
  git clone <repository-url> edgar-dashboard
  ```

Then open a terminal **in that folder**:

```powershell
cd C:\dev\edgar-dashboard
dir
```

You should see `docker-compose.yml`, `Dockerfile`, `app.py`, etc.

---

## Step 5 — Create your settings file

The SEC requires that requests identify a real contact, so set your name and
email.

```powershell
Copy-Item .env.example .env
notepad .env
```

Edit the file so it looks like:

```
HTTP_PORT=8000
EDGAR_IDENTITY=Your Name you@example.com
```

Save and close. (`HTTP_PORT` is the port you'll open in the browser — change it
if 8000 is already in use.)

---

## Step 6 — Build and start

Make sure Docker Desktop is running, then:

```powershell
docker compose up -d --build
```

The first build downloads a Linux image and the Python libraries — expect a few
minutes and a few hundred MB. When it finishes, open:

**<http://localhost:8000>**

Type a ticker (e.g. `AAPL`), press **Run**, and wait. The first run for a ticker
takes ~1–3 minutes; the **Recent** tab lists everything you've run and lets you
view or download the charts as a ZIP.

---

## Step 7 — Day-to-day use

```powershell
docker compose ps          # is it running?
docker compose logs -f     # watch output (Ctrl+C to stop watching)
docker compose stop        # stop (keeps data)
docker compose start       # start again
docker compose down        # stop and remove the container
docker compose down -v     # ALSO delete the SEC cache volumes
```

To **update** after getting new project files: replace the folder contents, then
`docker compose up -d --build` again.

Your charts also live on your PC under `financial_graphs\` in the project
folder, next to the compose file.

---

## Shortcut for non-technical users

The repo includes **`run-windows.cmd`**. Double-click it and it will:

1. create `.env` from the example if missing,
2. check Docker is installed and running,
3. build + start the dashboard,
4. open the browser.

If it says Docker isn't running, start Docker Desktop and run it again.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `docker : The term 'docker' is not recognized` | Docker Desktop isn't installed or isn't running. Install/start it and wait for the steady whale icon. |
| `error during connect ... dockerDesktopLinuxEngine` | The Docker engine is still starting. Wait a minute and retry. |
| `port is already allocated` / `bind: address already in use` | Another app uses that port. Set a different `HTTP_PORT` in `.env` (e.g. `8080`) and `docker compose up -d`. |
| `wsl --install` fails or WSL errors | Run `wsl --update`, ensure virtualization is enabled in BIOS, then restart. |
| `no configuration file provided: not found` | You're in the wrong folder. `cd` into the project directory (where `docker-compose.yml` is). |
| Build fails with TLS/timeouts | Check VPN/antivirus. Retry `docker compose build --no-cache`. |
| Build is extremely slow | Add an antivirus exclusion for the project folder, or run `docker compose build` once and reuse it. |
| Can't rename to `.env` in Explorer | Windows dislikes leading-dot names in Explorer. Use `Copy-Item .env.example .env` instead. |
| Changes to `.env` don't apply | Recreate the container: `docker compose up -d --force-recreate`. |
| "No parseable 10-K filings found for 'X'" | That ticker isn't a US 10-K filer (e.g. foreign company filing 20-F). Try a US-listed ticker. |
| WSL uses too much RAM | Optional: create `C:\Users\<you>\.wslconfig` with `[wsl2]` / `memory=4GB`, then `wsl --shutdown`. |

---

## FAQ

**Do I need Python or Git?** No. Docker bundles everything. Git is only needed
if you prefer cloning over a ZIP.

**Does it connect to someone else's server?** No. It calls SEC EDGAR directly
from your machine, using the contact you put in `EDGAR_IDENTITY`.

**Where do my charts go?** `financial_graphs\` in the project folder, and they're
downloadable from the web UI.

**How do I change the port?** Set `HTTP_PORT` in `.env` and run
`docker compose up -d`.

**How do I free disk space?** `docker compose down -v` removes the cached SEC
downloads; `docker system prune -a` removes unused images (you'd rebuild next
time).

**Is it safe?** It has no login, but it's only reachable from your own PC at
`localhost`. Don't expose it to the internet without a reverse proxy and a
password.
