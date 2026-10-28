# SEC EDGAR 10-K Data Extractor

> **Disclaimer — this project is absolutely vibecoded.** It was built for fun,
> largely with AI assistance, and is provided **as is**, with no warranty of any
> kind. I am not responsible for how you use it or for any result of using it.
> Nothing it produces is financial or investment advice: the numbers can be
> wrong, incomplete or stale, so check the original SEC filings before relying
> on anything.

Connect to SEC EDGAR, download the last ten 10-K filings for a company,
extract the XBRL financial data, and write it as CSV. The command line writes
into `financial_graphs/`; the web app keeps each user's results apart under
`data/users/<id>/`.
The run form accepts 3, 5, 10 or 20 filings; the API accepts any value 1–20.
Charts are written as data, `charts.json` (one spec per chart: years, series,
colours, formats, axes); `--no-charts` skips them. The web app draws the specs
with ECharts (bundled in `static/vendor`), with a years slider under each chart
covering the whole history; PNGs of the same specs are drawn by matplotlib only
when they are downloaded, in windows of 5 or 10 years or one wide image, light
or dark. Both use the app's own font (Ubuntu Mono, bundled in `static/fonts`
under the Ubuntu Font Licence) and colours, with no frame and only faint
horizontal gridlines.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The SEC requires a contact (name + email) in the User-Agent of every request.
In the web app that is the signed-in user's account name and email; nothing to
configure. On the command line, set it in the environment:

```bash
export EDGAR_IDENTITY="Jane Doe jane@example.com"
```

> The SEC asks for a real contact so it can reach you if your requests cause
> problems. Without `EDGAR_IDENTITY` the CLI falls back to a placeholder.

## Run

```bash
python edgar_dashboard.py --ticker AAPL --filings 10
python edgar_dashboard.py --ticker AAPL --no-charts   # data only (screener mode)
python edgar_dashboard.py --ticker AAPL --output-dir /tmp/out   # writes /tmp/out/AAPL/
```

## Output

Each ticker writes to its own sub-directory, so multiple runs never overwrite
one another:

```
financial_graphs/
├── NFLX/
│   ├── financial_data.csv   # per-year ratio inputs/outputs
│   ├── evaluation.csv       # per-year valuation, quality and distress scores
│   ├── insider.json         # open-market insider buys/sells, last 3 years
│   ├── statements.csv       # every dimension-free primary-statement line
│   ├── facts.csv            # every fact for each period, dimensions included
│   ├── notes.csv            # key schedules: debt, segments, EPS, taxes, shares
│   ├── charts.json          # the charts as specs (drawn by the page)
│   ├── calc_check.csv       # each linkbase total against the sum of its parts
│   ├── data_quality.csv     # per chart value: reported, derived or missing
│   └── .png_cache/          # web app only: PNGs drawn by a download, until the next run
├── AAPL/   # same CSVs
└── MSFT/   # same CSVs
```

The web app uses the same per-ticker layout inside each user's folder,
`data/users/<id>/<TICKER>/`, plus a `run.json` recording the last successful
run (filings, charts or not, finish time) for the same-day cache.

`statements.csv` / `facts.csv` / `notes.csv` are the data-first, XBRL-driven
artifacts: no metric is hard-coded, so a new filer needs no code change.
`financial_data.csv` holds the derived ratio inputs/outputs.

`evaluation.csv` (from `valuation.py`) adds per-year P/E, P/B, P/FCF, EV/EBIT,
EV/EBITDA, earnings yield, Greenblatt return on capital, the historical P/E
median and percentile, Piotroski F-score (0–9), Altman Z-score and the accruals
ratio. When a filer prints no total-liabilities line (the Z-score needs one),
it is derived: current plus noncurrent totals, else total liabilities and equity
(or total assets) minus equity. Prices come from Yahoo; if Yahoo is unreachable the run still succeeds
and valuation is skipped with a warning. `insider.json` (from `insider.py`)
lists every open-market Form 4 buy and sell (codes `P` and `S`) in the last 6
months — no transaction cap. Both steps are fail-soft, and the trades have
their own **Insiders** tab.
`charts.py` charts them as `08_valuation`: P/E history against its median,
annotated with the percentile, plus F-score and Z-score bars.

A run made before `charts.json` existed (PNGs only) is upgraded the first time
it is opened: the spec is built from its CSVs and the old PNGs are removed.

```bash
python edgar_dashboard.py --ticker NFLX
python edgar_dashboard.py --ticker AAPL
python edgar_dashboard.py --ticker MSFT
```

## Web UI (minimal)

A small local web front-end wraps the pipeline: type a ticker, watch the run,
then view or download the charts. Runs are queued and executed one at a time
(SEC is rate-limited); results persist in `data/users/<id>/`, so they are
available later from the **Recent** tab.

**Accounts are local.** The first page is a sign-in form; **Create account**
asks for a name, email and password. Accounts live in `data/stocky.db`
(SQLite, passwords hashed) and the session key in `data/secret_key`: delete
`data/` (or the app) and every account and result is gone. Users never see one
another's runs, jobs or files; each user has an internal id, never shown, that
names their folder. The account's name and email are the SEC identity for that
user's runs, so there is nothing to put in Docker or `.env`. Set
`STOCKY_DATA_DIR` to keep `data/` somewhere else.

```bash
pip install -r requirements.txt
python app.py            # http://localhost:8053
```

Routes:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The UI (Home / Charts / Insiders / Screener / Simulation / News tabs); redirects to `/login` when signed out |
| `GET` | `/login` | Sign-in / create-account page |
| `POST` | `/api/signup` | `{"name","email","password"}` → creates the account and signs in |
| `POST` | `/api/login` | `{"email","password"}` → signs in |
| `POST` | `/api/logout` | Signs out |
| `GET` | `/api/account` | The signed-in user's name, email and SEC identity (never the id) |
| `PATCH` | `/api/account` | Change `name` / `email` / `new_password` (email and password need `current_password`) or `color` (a preset name) |
| `GET` | `/api/account/avatar` | The profile picture (256×256 PNG), 404 if none |
| `PUT` | `/api/account/avatar` | Raw image body (PNG, JPEG or WebP, max 5 MB) → cropped to a square and stored |
| `DELETE` | `/api/account/avatar` | Removes the profile picture |
| `DELETE` | `/api/account` | `{"confirm":"DELETE","password"}` → deletes the account and all its data |
| `DELETE` | `/api/data` | `{"confirm":"DELETE"}` → deletes all of the user's results; the account stays |
| `POST` | `/api/run` | `{"ticker":"NFLX","filings":10,"fresh":false}` → queues a run, or `status: "cached"` |
| `POST` | `/api/cancel` | Cancels the user's queued/running jobs; keeps all files |
| `GET` | `/api/status/<ticker>` | queued / running / done / error + log tail |
| `GET` | `/api/jobs` | Active jobs (with `charts` flag) + last screener batch |
| `GET` | `/api/recent` | Tickers with results, newest first |
| `DELETE` | `/api/recent/<ticker>` | Deletes that ticker's results folder |
| `GET` | `/api/files/<ticker>` | Chart ids and data files of a ticker's run |
| `GET` | `/api/charts/<ticker>` | `charts.json`: every chart's spec, drawn by the page |
| `GET` | `/api/checks/<ticker>` | The calculation check: counts for statements and notes, and every total that does not add up with its parts |
| `GET` | `/api/facts/<ticker>` | Every statement line and plain fact of the filings' own periods (concept, label, unit, value per year), for the Charts search |
| `GET` | `/api/evaluation/<ticker>` | `evaluation.csv` rows for one ticker |
| `GET` | `/api/insiders/<ticker>` | `insider.json` trades for one ticker |
| `GET` | `/api/news/<ticker>` | Recent articles (title, source, date, link) from Google News; `?limit=20\|30\|40\|50` (default 20), `?days=1\|7\|14\|30` (default 14), `?scope=company\|market` (default company; market adds `topic`), `?fresh=1` skips the 30-min cache |
| `GET` | `/api/simulation/<ticker>` | Yahoo price history + dividends (`?range=1y`) |
| `GET` | `/api/fundamentals/<ticker>` | Flat metric set behind the screener |
| `GET` | `/api/screener` | Cached screener results, Greenblatt ranks |
| `POST` | `/api/screener` | `{"tickers":[...],"filings":10}` (max 25) → data only, no charts |
| `GET` | `/files/<ticker>/<name>` | View a chart inline |
| `GET` | `/download/<ticker>` | The charts (PNG, 5-year windows) and data files as a `.zip` |
| `GET` | `/download/<ticker>/all` | The download dialog's zip: `?charts=` and `?files=` (comma-separated; absent = all), `?years=5\|10\|0` (years per chart image; 0 = one image), `?theme=light\|dark`, `?extras=` any of `insiders,news,screener,debug` (absent = all). Charts go in `charts/` as `<id>_<from>_<to>.png`; `?months=`, `?news_limit=`, `?news_days=` follow the tabs' options |
| `GET` | `/download/<ticker>/<name>` | One file |
| `GET` | `/healthz` | Health probe: version, commit, code hashes |
| `POST` | `/api/diag` | Browser errors, for the diagnostics log (sent by the page itself) |

The tabs sit on the left of the ticker bar's row, the bar on the right (on
narrow screens the tabs get a row of their own below the bar); the **Recent**
clock icon sits left of the ticker box, or at the right end of the tab row on
narrow screens. The logo and the Stocky name open Home. The logo and every
icon are drawn for this project (no third-party icon sets).
The address bar follows the open tab and the pinned ticker (`/#charts/AAPL`,
`/#screener`), so Back and Forward step through tabs, a reload stays put, and a
bookmark or shared link opens that tab and ticker (signing in first if
needed). A URL ticker wins over the saved pin; with no tab in the URL the
Options default is used.
**Home** (the house icon, first tab) is the default tab: an overview of the
latest search (the pinned ticker). It shows that company's key numbers (the
only place outside the Screener with stats), its spot in the screener's Magic
Formula ranking with the two tickers above and below (names only, click one to
switch to it), then one small thumbnail per chart (its last five years) that
opens in the Charts tab, the top five market headlines (see the News tab
below) and the five latest insider trades (with a link to the Insiders tab).
Running a single ticker switches to Home; the run's progress is in the Charts
tab and its log in the Logs tab.

**Logs** (`/#logs`, the terminal icon in the tab row; a queued or running
badge and "Open the log" under the Charts header open it too) shows the whole
log of a run: pick the ticker (the running one is preselected); every line is
timed (`15:45:44 [*] Filing 2/20: 10-K for the year ended 2025-06-30,
downloading and parsing its XBRL`) and the view updates every 1,5 s while it
runs. The pipeline says what it is doing at each step: looking up the company,
listing filings, each filing's download and parse time and linkbase result,
insider Form 4s read so far, prices and exchange rates, the ADS ratio, every
chart. Its icon can be moved like any tab (Options → General → Tab order).

**Downloads.** Every tab has a download icon and downloads what it shows, with
the options chosen there. **Home** and **Charts** open a dialog with a checkbox
per chart, per data file and per extra (insider trades, news, the screener row,
a `_debug/` folder for bug reports), plus the years per image (5, 10 or all)
and the theme of the PNGs. Home starts with everything ticked, Charts with
what was ticked last time, and a chart's own download icon with just that
chart. The PNGs are drawn on the first download of a run (a few seconds) and
reused after that. **Insiders** a CSV of the trades in the
chosen period and side (buys, sells or both), **Screener** a CSV with every field of every ticker in the
current sort order, **News** a CSV of the listed articles (headline, outlet,
date, link; no article text), **Simulation** a PNG of the price chart with the
projection's settings and results under it.

The ticker bar decides the mode. **One ticker** runs the full pipeline: charts
plus screener data. It is pinned to the **Charts** tab and stays there until you
run/open another ticker; the pin survives page reloads and cancel. Its scores
and ratios live in the **Screener** tab only. The Charts tab is empty until
then. The line under the header shows the latest log line while the run is
queued or running, and the error if it fails, with a link to the Logs tab. The
chart index sits inside the Charts panel, one line per chart. Each chart shows
as many recent years as fit its width; the years slider under it moves the
window through every year in steps of exactly one year (no gliding): its
handle is as wide as the window, with a tick per position, and the arrows
either side (or the keyboard's arrow keys on the slider) step one year. The
subtitle names the years on show and the axis rescales to them at each step.
A line keeps going past the window's edges toward the years off screen,
without their numbers, so you can see where it comes from and goes. Hovering a year lists every value; legend keys hide and show a
series. The **search** above the index looks for a fact (goodwill, buybacks,
income taxes paid…) in the charted series and in every statement line and fact
of the filings; pick a year beside it, or type it ("goodwill 2019"). A charted
fact scrolls to its chart, slides that year into view and opens its values;
any other shows its values per year, with **Draw as a chart** to chart it until
the page is reloaded. Charts show one per row, as wide as the page column.
**Several tickers** switch to screener mode:
data only, no charts (`--no-charts`), and the pinned graph is left as is.
Running a single ticker that is queued as a screener job upgrades it to a graph
run; if it is already running, a graph run is queued after it. The **Insiders**,
**Simulation** and **News** selectors switch to the pinned ticker whenever the
pin changes. The **Insiders** tab filters buys, sells or both, as well as the period. Clicking a ticker in the **Screener** opens it on Home and pins
it, so every tab shows it. **News** lets you pick 20, 30, 40 or 50 articles
from the last 24 hours, 7, 14 or 30 days. The cancel button (✕ beside Run)
only shows while a chart run or a screener batch is queued or running. A reloaded page resumes the pinned run, other graph runs and the
screener batch's progress (`/api/jobs`).

**Same-day cache.** Asking for graphs a ticker already got today, with the same
number of filings, shows that generation instead of re-running the pipeline. A
bar drops from the top saying so, with a **Generate fresh** button and a
visible 3-second countdown, then closes itself. Each successful run records
what it produced in `run.json` next to its files; a screener run (no charts)
never counts as cached graphs. The ticker bar's placeholder suggests a few
random well-known tickers and changes every few seconds.

### Options

The gear button right of the theme button opens **Options**: a small dialog in
the middle of the screen with a search box on top, vertical tabs on the left
and their sections on the right. Searching shows every matching option across
all tabs. Clicking your name in the header opens it on the **Account** tab.

- **Account**: change name, email or password (email and password need the
  current password); profile colour and picture; see the SEC identity, sign
  out, delete the account. The profile colour (orange by default; eight
  presets, each with a dark- and a light-theme shade of at least 6:1 contrast)
  colours every panel title, section heading and link, and fills the avatar
  behind your initial, or rings your picture. Colour and picture belong to the
  account (stored in `data/stocky.db` and `data/avatars/`), so they follow you
  to any browser; **Delete all results** keeps them, deleting the account
  removes them.
- **General**: tab shown on open (Home by default), tab order (up/down
  arrows, **Reset order**; Recent stays beside the ticker box).
- **Appearance**: dark or light theme; text size (Small / Normal / Large /
  Larger: every font scales through the `--fs` CSS variable); date format
  (31/12/2026, 12/31/2026 or 2026-12-31); number format (1,234.5 / 1.234,5 /
  1 234,5).
- **Defaults**: number of 10-K filings per run (also used after cancel);
  News articles and period (20 from 14 days unless changed);
  Insiders period (the tab's own picker changes it for that
  visit only); Simulation investment, horizon, dividends (cash or reinvest)
  and price range.

These settings live in the browser (`localStorage`, key `stocky-prefs`), not
in the account: another browser or device starts from the defaults.
- **Data**: what is stored, and **Remove all data**.

Removing all data and deleting the account both open a confirmation screen
listing exactly what goes, and need `DELETE` typed (plus the password, for the
account) before the button enables. Both stop the user's running jobs first.

The **Screener** tab posts up to 25 tickers, shows a sortable table of scores and
ratios, and caches each ticker's result for 7 days. Tickers with no cached data
are queued through the same serialized worker as `/api/run`. It ranks the
watchlist with Joel Greenblatt's Magic Formula: **earnings yield** (EBIT / EV)
and **return on capital** (EBIT / (total assets − current liabilities)); the
combined rank is 1 + 1 for the best of both, lower is better. Each row has a
**charts** button that opens the ticker in the Charts tab, running the full
pipeline first only if it has no charts yet. Older `evaluation.csv` files without `return_on_capital` are repaired
on the fly from `financial_data.csv` + `statements.csv`.

The **Insiders** tab lists every open-market Form 4 buy/sell (the SEC filing
officers, directors and 10% owners make within two business days of a trade),
newest first, with a period selector (1 month to 3 years, default 1 year,
remembered per browser) and a ticker selector. The count and the collection
date share the line under the header. `insider.py` collects 3 years and does
not cap transactions (it stops at the window, with a 1500-filing safety valve;
`since` in `insider.json` says how far back the data really goes).
The Form 4s download in the background while the 10-Ks are fetched, since
neither needs the other. Runs made
before this collected 6 months; the tab says so and suggests re-running.
A trade's value is shares × the reported price. Sales made in several lots
often report a price of 0 and give the range only in a footnote ("weighted
average … $X to $Y"); the value then uses the midpoint, shows as **≈** and has
`value_estimated: true`. Form 4s the company files as the insider of *another*
issuer (a stake it holds, an acquisition) are left out.

Every tab's panel title is just the tab name, every refresh button is the
circular-arrow icon, and every ticker drop-down reads `TICKER (last run date)`.

The **News** tab lists up to 25 articles about the company from the last 14
days: headline, outlet and date, each linking to the original. The company name
comes from Yahoo's search and the articles from the Google News RSS feed, in
Google's ranking order (its "top stories", the closest free signal to
popularity). Results are cached for 30 minutes and shared by every user;
the refresh button fetches them again. Nothing else is derived from them.

Home's headlines (`scope=market`) are about the stock rather than the company:
the ticker with market words (shares, price target, analyst, earnings), its
sector and industry from Yahoo's search, and the wider market and economy
(Wall Street, the Fed, inflation, rates). The three feeds interleave stock,
stock, sector, market, and each headline is labelled with its topic; product
and other company news stays in the News tab. The sector feed is skipped when
Yahoo does not know the ticker's sector.

### Tests

```bash
python -m unittest discover tests
```

Queue and route tests for `app.py`; the pipeline is replaced by a fake script,
so they need no network and finish in a few seconds.
GitHub Actions runs them on every push and pull request
(`.github/workflows/tests.yml`, Python 3.12 like the Dockerfile).

### Simulation tab

The **Simulation** tab charts the selected ticker's price from Yahoo (separate
from EDGAR; it never writes to `data/`). It has two modes:

- **Projection** — set a target price, an investment amount, and a horizon. The
  dashed line projects the price, with dividends either paid in cash or
  reinvested.
- **Backtest** — click any historical point on the chart to invest the amount
  at that close and value it today. The projection line stays visible but turns
  grey and is disabled, as are target price and horizon. Dividends can be paid
  in cash (default) or reinvested at the payment-date price.

The backtest lists one row per full year held (plus a final **Now** row):
dividends received that year, portfolio value, gain in that year, and the
running total gain. Clicking a future point or **reset** returns to projection.
Estimates only; no taxes or fees.

The red-X **cancel** button beside **Run** stops your queued/running pipeline
jobs, aborts in-flight browser requests, and resets the interface to its initial
state, except for the pinned graph. It never deletes generated files; run logs
and partial results stay in `data/users/<id>/`.

## Docker

A single service that publishes one HTTP port:

> **Windows users:** see **[WINDOWS.md](WINDOWS.md)** for a from-scratch guide
> (Docker Desktop + WSL2) — no Python or Git required. After Docker is installed,
> double-click `run-windows.cmd` and it creates `.env`, builds, starts, and opens
> the browser.

```bash
docker compose up -d --build     # http://localhost:${HTTP_PORT:-8053}
```

### Dockge

1. Put this project (or at least `docker-compose.yml` + `.env`) in a stack
   directory Dockge manages.
2. `cp .env.example .env` and edit it:
   - `HTTP_PORT` — the host port to publish (default 8053); or just edit the
     `ports:` line in the compose file directly.
3. Deploy in Dockge. If the compose file is not next to the Dockerfile, point
   the build at the repo:

   ```yaml
   services:
     dashboard:
       build:
         context: /path/to/10kanal-yzer
         dockerfile: Dockerfile
   ```

Then attach your own nginx (below). No certificate or reverse-proxy containers
are bundled on purpose — you handle that side.

### Your own nginx

`nginx/dashboard.conf` is a ready server block. Copy it, set `server_name`, make
`proxy_pass` match your published port (default 8053), and reload:

```bash
sudo cp nginx/dashboard.conf /etc/nginx/conf.d/dashboard.conf
sudo nano /etc/nginx/conf.d/dashboard.conf     # server_name + proxy_pass port
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dash.example.com       # optional TLS
```

Want a login? Create a password file and uncomment the `auth_basic` lines:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd you
```

### Notes

- `./data` (accounts and results) is bind-mounted to `/app/data`; the
  container runs as uid 10001, so that directory must be writable by it (see
  "Container permissions" below). Change it with `STOCKY_DATA` in `.env`.
- edgartools' raw SEC downloads are cached on the host: the compose file
  bind-mounts `./edgar_cache` and `./edgar_data` (`EDGAR_CACHE` / `EDGAR_DATA`)
  to `/home/appuser/.edgar_cache` and `/home/appuser/.edgar`, making repeat runs much faster.
- The app listens on `0.0.0.0:8000` inside the container (override with `PORT`).
  Its sign-in is meant for a private machine or network, not the internet.
- Runs are serialized (one at a time); ~1–3 minutes per ticker the first time.

### Rebuilding / clean redeploy (Dockge)

Dockge's **Update** button only pulls images, so it does nothing for a stack
that uses `build:` (see Dockge discussion #267). This stack builds its image
locally, so rebuild it from the stack's **Web Terminal** (or over SSH in the
stack directory):

```bash
docker compose down                 # remove the container
docker image rm edgar-dashboard     # delete the built image
docker compose build --no-cache     # rebuild from scratch
docker compose up -d                # start it again
```

- Code-only changes usually just need `docker compose up -d --build`.
- `docker builder prune` clears leftover build cache if you want the space back.
- The SEC download cache lives on host bind paths, not named volumes, so
  `docker compose down -v` leaves it (and `./data`) untouched.
- If you removed `image:` from the compose file, check the generated name with
  `docker compose images`.

### Container permissions (the container is NOT root)

The image runs as `appuser`, **uid 10001** (since the 2026-10 hardening). Every
host path bind-mounted into it must be writable by that uid, or the pipeline
runs, reports success in its own log, and the app still sees nothing:

```bash
mkdir -p data edgar_cache edgar_data      # Docker would create them root-owned
sudo chown -R 10001:10001 data edgar_cache edgar_data
```

- The SEC cache is written to `~/.edgar` / `~/.edgar_cache`, so the compose file
  sets `EDGAR_LOCAL_DATA_DIR` / `EDGAR_CACHE_DIR` to `/home/appuser/...` — keep
  those env values and the mount targets in sync if you move them.
- Diagnose in one request: `curl -s localhost:8053/healthz` prints a `data`
  block — `graph_dir`, `exists`, `readable`, `writable` and `uid` for the data
  directory (no tickers: those are per user). `writable: false` there is your
  answer; sign-up also fails then, because the account database cannot be written.
- The UI does the same: if a run finishes but `/api/files/<ticker>` finds
  nothing, the results area shows a card with the user's folder, the uid and the
  visible tickers instead of staying blank. A run that cannot even create its output
  directory fails immediately with
  `cannot write results dir <path>: PermissionError … (uid=10001; check the
  data bind mount ownership)` rather than hanging on *queued*.

### Deploy checklist (read this before adding a module)

**The `Dockerfile` copies every root Python module with `COPY *.py ./`. Do not
replace that with an explicit file list.** A hard-coded list is how a new module
(`valuation.py`, `insider.py`) gets left out of the image: the container then
runs a current `app.py` with a stale pipeline, the pipeline prints
`[!] valuation skipped: ModuleNotFoundError`, no `evaluation.csv` is written,
and the screener table stays empty with no obvious error.

After every deploy, confirm what is actually running:

```bash
sudo git pull                        # then build + up; do NOT chain with && silently
sudo docker compose build --no-cache && sudo docker compose up -d
curl -s localhost:8053/healthz
```

`/healthz` must report the pipeline modules and the exact code hashes:

```json
{"status":"ok","version":"1.0.0","commit":"…","dirty":null,"code_sha1":"…",
 "features":["valuation","insider","calc_check"],"pipeline_sha1":"…","charts_sha1":"…",
 "data":{"graph_dir":"/app/data","exists":true,"readable":true,
         "writable":true,"uid":10001}}
```

- `features` missing `valuation` → the image was built without that module
  (wrong build context, or a stale/edited Dockerfile).
- `data.writable: false` or `data.readable: false` → the bind
  mount is not usable by uid 10001; fix ownership (§ "Container permissions").
- `pipeline_sha1` / `charts_sha1` must equal
  `sha1sum edgar_dashboard.py charts.py` **in the build context directory**.
  Different hashes → the container is not running the code you pulled.

Two failure modes to watch for, both silent:

- `git pull && docker compose build && docker compose up -d` stops at the first
  failure — a dirty working tree means no rebuild and the old container keeps
  serving. Run the steps separately or check the pull output.
- `docker compose up -d` **without** `build` reuses the existing image; code
  changes need `up -d --build` (or an explicit `build`).

## Versions and bug reports

The release number is in `VERSION` as `major.minor.patch`: **patch** for a fix,
**minor** for a new feature or visible change, **major** for a change that
breaks stored data or the API. Each release is tagged `vX.Y.Z`.

The running build is shown in Options → Data → Version, in `/healthz`
(`version`, `commit`) and in the name of Home's zip
(`<TICKER>_everything_v1.0.0_<commit>.zip`). Docker has no `.git`, so the
commit is optional there: set it with
`STOCKY_COMMIT=$(git rev-parse HEAD) docker compose up -d --build`; outside
Docker the app asks `git`. Since every release bumps `VERSION`, the version is
enough to find the code, and `code_sha1` (a hash of every root `*.py` and
`templates/*.html`) confirms it is exactly that release.

Every account has a diagnostics log, `data/users/<id>/diagnostics.jsonl`
(trimmed to the newest 2000 entries past 1 MB): failed requests (with the
traceback when the server crashed), failed pipeline runs (with the log tail) and
browser errors (script errors, failed fetches; at most 50 per page load).
Home's download adds a `_debug/` folder with `build.json` (version, commit,
Python and package versions), `jobs.json`, `diagnostics.jsonl`, every ticker's
last `run.log` and a `README.md` explaining them, so a bug can be reported by
sending that one zip. "Remove all data" deletes the log too.

## Metrics extracted per fiscal year

| Metric | XBRL concept(s) | Statement |
| --- | --- | --- |
| Revenue | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `RevenueFromContractWithCustomerIncludingAssessedTax`, `SalesRevenueNet`, `SalesRevenueGoodsNet` | Income |
| Cost of Revenue | `CostOfRevenue`, `CostOfGoodsAndServicesSold`, `CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization`, `CostOfGoodsSold`, `CostOfServices` | Income |
| Net Income | `NetIncomeLoss`, `NetIncomeLossAvailableToCommonStockholdersBasic`, `ProfitLoss` | Income |
| Gross Profit | `GrossProfit` (fallback: `Revenues - CostOfRevenue`) | Income |
| SG&A | `SellingGeneralAndAdministrativeExpense`, `SellingGeneralAndAdministrative`, `SellingAndMarketingExpense`, `GeneralAndAdministrativeExpense` | Income |
| R&D | `ResearchAndDevelopmentExpense`, `ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost` | Income |
| Goodwill Impairment | `GoodwillImpairmentLoss`, `GoodwillAndIntangibleAssetImpairment`, `AssetImpairmentCharges` | Income |
| Operating Income | `OperatingIncomeLoss` | Income |
| Operating Cash Flow | `NetCashProvidedByUsedInOperatingActivities`, `...ContinuingOperations` | Cash Flow |
| CapEx | `PaymentsToAcquirePropertyPlantAndEquipment`, `PaymentsToAcquireProductiveAssets` | Cash Flow |
| Current Assets | `AssetsCurrent` | Balance Sheet |
| Current Liabilities | `LiabilitiesCurrent` | Balance Sheet |
| Long-Term Debt | `LongTermDebt`, `LongTermDebtNoncurrent`, `LongTermDebtAndCapitalLeaseObligations` | Balance Sheet |
| Stockholders' Equity | `StockholdersEquity`, `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` | Balance Sheet |

Derived in-script: **Gross Margin**, **Operating Margin**, **Net Margin**
(`NetIncomeLoss / Revenues`), **Free Cash Flow** (`OperatingCashFlow - CapEx`),
**Current Ratio** (`AssetsCurrent / LiabilitiesCurrent`), **Debt-to-Equity**
(`LongTermDebt / StockholdersEquity`). All monetary values are in millions of
the filer's reporting currency (USD for a 10-K; see § Foreign filers).

### Balance-sheet and debt extraction

The income and ratio charts use the curated concepts in the table above. The
**balance sheet** (assets / liabilities & equity) and **debt** charts do not
hard-code lines: `extract_balance_lines()` reads the filer's own presentation
linkbase through edgartools' standardized `balance_sheet()` statement. The
library maps each tagged line onto a `standard_concept`, absorbing the
filer-specific and extension tags that vary company to company, and we keep one
row per dimension-free line. The charts stack every line the filer states
(totals are skipped when stacking because they are another line's parent) and
flag debt-like and cash-like lines by concept/label text. So a new company works
without adding tags by hand; only the curated income/ratio series need
maintenance.

## Notes on the data

- `amendments=False` excludes `10-K/A` amendments so each fiscal year appears once.
- **Cost of revenue** is the filer's total when it reports one. When it does
  not, or the value found is just one of its lines, it is the sum of the income
  statement's cost lines: those edgartools files as cost of goods and services
  sold, those tagged `CostOf…`, and the costs named after a revenue stream
  (Oracle reports "Cloud and software", "Hardware" and "Services" expenses
  beside the revenues of the same names, with no total, and in FY2010 tagged
  its hardware cost alone as `CostOfRevenue`). Gross profit is then revenue
  minus that cost, unless the income statement itself shows a gross profit
  line: a gross profit tagged only in a segment note is not used.
- Duration facts are restricted to periods of **≥ 300 days**. Without this, the
  quarterly facts tagged in the notes (which share a `period_end` with the
  annual figure) can be selected instead of the full-year value.
- After a merger, a filer often tags the consolidated statements under a
  successor/predecessor reporting basis (`us-gaap:StatementScenarioAxis`). There
  is then no dimension-free annual fact, so the extractor falls back to a
  scenario-only fact as the consolidated value (KHC's FY2015 successor year).
  That axis marks a period's basis, not a line-item breakdown, so it is safe.
- The current year is located by matching each fact's end date to the filing's
  `period_of_report`, **not** by the dataframe's `fiscal_year` column. For
  non-calendar fiscal years that column mislabels prior-year comparatives
  (Apple's FY2017 annual revenue is tagged `fiscal_year=2018`), which would
  silently duplicate years.
- Filings are de-duplicated by the exact `period_of_report` date, not by
  calendar year. Companies on a 52/53-week calendar can have two fiscal periods
  ending in the same calendar year (TTM Technologies has periods ending
  2024-01-01 and 2024-12-30); year-keyed de-duplication silently dropped one.
- Fiscal-year labels are numbered backwards from the newest filing's
  `dei:DocumentFiscalYearFocus`, stepping one year per ~annual period. That
  keeps the axis contiguous even when the DEI tag or the period's calendar year
  is individually unreliable (TTM's DEI focus skips 2022 and its period years
  skip 2021). The chosen year is stored in `fiscal_year`.
- `GrossProfit` is used when a filer tags it annually; otherwise it is derived
  as `Revenues - CostOfRevenue`. Netflix only tags `GrossProfit` quarterly, and
  biotech filers like Aquestive often have no cost of revenue at all, so some
  gross-margin points are legitimately absent. The CSV flags derived rows via
  `gross_profit_derived`.
- Each metric tries several XBRL concepts in priority order, because filers
  change tags over time. A company can stop tagging `NetIncomeLoss` from a given
  year (Estée Lauder moved to `NetIncomeLossAvailableToCommonStockholdersBasic`),
  tag only the total-equity variant once it has non-controlling interests, or
  use `LongTermDebtAndCapitalLeaseObligations` instead of `LongTermDebt`.
- When a filing contains the same fact twice (a rounded copy and a precise one),
  the value with the greatest decimal precision is used.
- Unparseable or missing filings are skipped with a warning rather than
  aborting the run, so one bad document cannot sink a whole dashboard.
- Companies with no EDGAR annual filings at all (e.g. Volkswagen, which files
  in Germany only) are out of scope.

## Foreign filers (20-F / 40-F, IFRS)

Foreign companies listed in the US file a **20-F** (or a **40-F** from Canada)
instead of a 10-K, mostly under **IFRS**, in their own currency: SAP in EUR,
TSMC in TWD, Toyota and Sony in JPY, Novo Nordisk in DKK. Some report under US
GAAP but still in euros (ASML). The pipeline reads them like a 10-K:

- **Currency.** Each filing's reporting currency is the one most of its money
  facts are in (`currency` in `financial_data.csv`, `facts.csv` and
  `charts.json`). Values stay in it, in millions, as the filer reports them;
  charts, tooltips and the search label them (€29,70B, NT$3,81T, ¥48,04T).
  Facts in another currency are ignored: TSMC also tags a USD "convenience
  translation" of its latest year, and edgartools sometimes renders that copy
  in a statement (TSMC's 2019 equity), which is swapped back for the TWD figure.
- **IFRS tags.** The curated metrics, the chart lines and the valuation inputs
  list the IFRS spellings next to the US-GAAP ones (`Revenue`, `CostOfSales`,
  `FinanceCosts`, `ImpairmentLossRecognisedInProfitOrLossGoodwill`,
  `PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities`,
  `NoncurrentLeaseLiabilities`, `PaymentsToAcquireOrRedeemEntitysShares`,
  `DividendsPaidClassifiedAsFinancingActivities`,
  `AdjustmentsForSharebasedPayments`, `NumberOfSharesOutstanding`, ...). Lines
  edgartools gives no `standard_concept` are matched by their tag. When no
  total is tagged, **SG&A** is selling (distribution) plus administrative
  costs, and IFRS **long-term debt** is bonds plus long-term bank loans.
- **Valuation.** The New York listing is usually an **ADS** priced in USD that
  stands for some number of ordinary shares (TSMC 5, Toyota 10, SAP 1). Each
  year's price is converted to the reporting currency at the fiscal-year-end
  exchange rate (Yahoo, e.g. `TWDUSD=X`) and divided by that number, so market
  cap, P/E, EV/EBIT and the Z-score compare like with like. The ratio comes
  from the 20-F cover page ("each representing five ordinary shares"), else
  from earnings per ADS over earnings per share in the filing, else from
  Yahoo: quoted price x ordinary shares / Yahoo's market cap, then its trailing
  EPS per ADS against the last annual EPS (both snapped to a usual ratio: 1, 2,
  3, 4, 5, 10, 20...). When none works, the price-based ratios are left blank
  rather than wrong. A company-specific capex line counts when it is the only
  investing line naming PP&E or capital expenditure (Toyota, Shell, Alibaba). The run log says which: `Valuation: USD price per ADS = 5 ordinary
  shares (eps), converted to TWD`. `evaluation.csv` keeps the quoted price and
  names both currencies (`currency`, `price_currency`).
- Foreign private issuers need not file Form 4, so the Insiders tab is often
  empty for them (TSMC's officers file voluntarily).

## Calculation check

Every filing ships a **calculation linkbase**: for each total on its
statements and notes (total assets, gross profit, operating cash flow...) the
lines that make it up and their sign. `calc_check.py` takes the filing's own
period values (dimension-free, in the reporting currency) and checks each total
against the weighted sum of its parts, allowing for rounding (half a unit of
each value's precision). A total that fails within its own statement is tried
again with its parts from every statement (TSMC splits profit and other
comprehensive income across two). Results go to `calc_check.csv`, the run log
and the **Calculation check** section under the Charts tab's index:

- **ok**: the total adds up.
- **mismatch** ("off by"): every part has a value and the sum still differs, so
  a line is tagged in an unexpected way or missing from the linkbase. These are
  the ones worth a look: they name the line the curated metrics may be missing.
- **incomplete** ("untagged parts"): some parts have no plain value that year
  (tagged only per segment or tranche), so the sum cannot close.

A mismatch says why when one simple fix explains it: the total (or one part)
is tagged with the wrong sign, a part does not belong in the total, or a part
is listed next to its own parts so they count twice. Oracle shows all three:
its FY2024 current tax is filed as −3 413M (SEC's own XBRL API has the same
value), and its FY2020 linkbase puts operating income and the revenue and
costs it is made of under income before taxes. These are the filer's errors,
not the extractor's. While a ticker runs again, its old check is hidden until
the new run is done.

The section is one line (the counts) until opened. Statement totals come
first; note tables, where parts are often tagged only per category, are folded
away. Opening a row lists every part and its value.
For example, SAP's 8 filings add up in all 295 statement totals; TSMC's show
its comprehensive income without the profit line in its linkbase, and
investing cash flows with parts it tags only in some years.

## Verification

`verify_facts.py` re-fetches the extracted facts from SEC's own XBRL API
(`data.sec.gov/api/xbrl/companyconcept/...`) and compares them to the dashboard
year by year, matching each year to the exact accession it came from:

```bash
python verify_facts.py                 # default sample
python verify_facts.py NFLX AAPL MSFT
```

It exits non-zero if any value differs. It now covers the income-statement
lines (revenue, cost of revenue, gross profit, SG&A, R&D, goodwill impairment,
operating income, net income) as well as the balance lines used by the ratio
charts. Derived gross profit is checked internally as `Revenue - CostOfRevenue`.
Run it after generating a ticker to confirm the charts rest on verified numbers.
All checks pass exactly (e.g. KHC 125/125, the default sample 284/284).

> The fallback for successor/predecessor reporting bases (§ Notes) produces
> values that SEC's `companyconcept` API does not expose, so those specific
> year-values are skipped rather than compared. Everything the API does expose
> is matched exactly.

## Finding cheap companies

- Run a watchlist (up to 25 tickers) through the **Screener** tab.
- Magic Formula: rank by earnings yield (EBIT/EV) and return on capital
  (EBIT/(assets − current liabilities)); the lowest combined rank is the
  cheapest quality company on the list.
- Quality: require Piotroski F-score ≥ 7.
- Safety: require Altman Z-score > 2.99 (below 1.81 is distress).
- Cheapness: rank by low P/E percentile — cheap against its own history, not
  the market — and by high FCF yield.
- Cross-check insider buys (Form 4 code `P`); sells (code `S`) need a reason.
- These scores are mechanical screens, not recommendations.

## Security & reliability hardening (2026-10)

A full audit of the AI-generated codebase led to these changes. Agents editing
this project must not regress them.

**Web layer (`app.py`, `templates/index.html`)**

- Runs are killed after a 30-minute subprocess timeout; previously one hung
  SEC fetch held the global run lock forever and froze the pipeline.
- `DELETE /api/recent/<ticker>` re-checks for an active run under the job lock
  immediately before `rmtree` (TOCTOU race).
- `/api/simulation/` responses are cached 60 s per ticker+range; previously
  every poll proxied Yahoo live (abuse amplifier + rate-limit risk).
- All responses send `X-Content-Type-Options: nosniff`, `X-Frame-Options:
  DENY`, `Referrer-Policy: no-referrer` and a CSP (`default-src 'self'`, inline
  script/style allowed for the single template).
- The UI escapes server/API strings before `innerHTML` (`esc()` helper).
- Local accounts (2026-10): every route except `/login`, sign-up/sign-in,
  `/healthz` and `/static` needs a signed-in session; passwords are hashed
  (werkzeug), the session cookie is HttpOnly + SameSite=Lax, and destructive
  endpoints need a JSON `{"confirm":"DELETE"}` body, which a cross-site form
  cannot send. Jobs, files and cancel are scoped to the user.

**Pipeline (`edgar_dashboard.py`, `xbrl_extract.py`, `charts.py`)**

- Filing list is pre-filtered for unreadable `period_of_report` *before*
  sorting; the sort used to raise outside the per-filing guard and abort runs.
- A filing's statement/fact rows are committed only after the filing fully
  succeeds; partial rows used to produce NaN `fiscal_year` and crash every
  chart (`astype(int)` on NaN). Charts now use nullable `Int64` + `dropna`.
- A present-but-dirty `dei:DocumentFiscalYearFocus` falls back to the period
  year instead of sinking the filing; per-row parse errors in statement
  extraction skip the row, not the filing.
- `dei:EntityCommonStockSharesOutstanding` (cover-page instant, after period
  end) is now kept from the nearest instant within 45 days after period end;
  the shares chart previously had a dead first candidate.
- Chart concept aggregation is priority first-match per year (same rule as the
  extractor); summing all candidates double-counted combined + component
  concepts. dei share counts are dropped for years with us-gaap share data.
- Fallback palette colours are md5-based; Python's `hash()` differs per
  process and changed chart colours run to run.
- Chart rendering closes all figures in a `finally`; an exception mid-chart
  no longer leaks figures.
- All CLI tools validate tickers against `^[A-Z][A-Z0-9.\-]{0,9}$` (path
  traversal via `--ticker ../../x` wrote outside `financial_graphs/`).
- Silent `except: continue/return` paths (form list, facts frame) now print
  warnings instead of masquerading as "no data".

**Verifier (`verify_facts.py`, `crosscheck.py`)**

- The SEC API User-Agent is built from `EDGAR_IDENTITY` at runtime; a
  hardcoded placeholder was always sent before.
- Network failures are `[ERROR]` and fail the run (exit non-zero); HTTP 404
  (company never tagged the concept) remains a SKIP. One ticker's crash no
  longer aborts the rest.
- 20-F/40-F filings are verified too (the extractor already handles them).
- `gross_profit_derived` is parsed string-safely (`bool("False")` is `True`).

**Deployment (`Dockerfile`, `docker-compose.yml`, `nginx/`, requirements)**

- **Port contract: 8053 everywhere** — compose `${HTTP_PORT:-8053}:8000`,
  `.env.example`, nginx `proxy_pass`, Windows launcher, `python app.py`
  default. 8000 is only the container-internal port.
- Container runs as non-root user `appuser` (uid 10001); `mem_limit` raised to
  2g (1g OOM-killed pandas mid-run, leaving partial CSVs).
- The dead `edgar_cache` / `edgar_data` named volumes were removed; caches
  are host bind mounts.
- The image serves Flask via `waitress`, not the dev server.
- The image copies **every** root module (`COPY *.py ./`). Never use an explicit
  file list — that is how `valuation.py` was left out of a build and the
  screener silently produced nothing. See § "Deploy checklist".
- `requirements.txt` is pinned to exact tested versions (auto-deploy builds
  on every push; unpinned deps were unreviewed supply-chain exposure).

Known remaining exposure: the sign-in has no rate limiting or lockout and there
is no HTTPS of its own — it is meant for a private machine or network. Put it
behind nginx with TLS (and `auth_basic` if you like) before exposing it.

## License

Copyright 2026 Jorge Canut. Licensed under the
[Apache License 2.0](LICENSE): free to use, modify and share, including
commercially, **as long as you give credit**. Keep the [`NOTICE`](NOTICE) file
and its attribution (to Jorge Canut, with a link to this repository) in any
copy or derivative you distribute, in your documentation or in an about screen.
The License's warranty disclaimer and limitation of liability apply in full.
