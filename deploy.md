# Free 24/7 deployment

## Option A - Render (no card needed)  <- recommended start
1. Push this folder to a **GitHub repo**.
2. Render dashboard -> New -> **Blueprint** -> pick the repo (it reads `render.yaml`). Plan = Free.
3. After first deploy copy the URL (`https://pk-toll-api-xxxx.onrender.com`) and add env var
   `KEEP_ALIVE_URL = <that URL>/health` (Render -> Environment).
4. Create a free monitor at **UptimeRobot** (or cron-job.org): HTTP(s), URL `<your URL>/health`, every 5 min.
   Optional 2nd monitor, type *Keyword*, URL `<your URL>/api/v1/rates/status`, keyword `"stale":false`
   -> you get an alert if rate checking silently breaks.
5. GitHub repo -> Settings -> Actions -> allow workflows. The "Update toll rates" workflow now runs every 6 h.

Why it stays alive: Render free services sleep after 15 min without traffic and wake in ~1 min.
The UptimeRobot ping (5 min) + the app's own self-ping (10 min) means it never idles.
Free quota is 750 instance-hours/month; a 31-day month is 744 h, so ONE always-on service fits - don't add a second free service in the same workspace.

## Option B - Northflank Sandbox (no sleeping, card verification)
Free tier advertises always-on compute, 2 services, 2 cron jobs. Create a service from the repo using the `Dockerfile`,
port 8000, set the same env vars. Card is only verified, not charged (check their current terms).

## How prices change automatically (3 independent layers)
| Layer | Runs | What it does |
|---|---|---|
| In-app updater | on boot + every 6 h | reads NHA pages, validates, applies instantly |
| GitHub Action | every 6 h | same check; commits `rates_live.json` -> Render redeploys. Survives restarts/ephemeral disk; **emails you if no source is readable** |
| Seed file | always | `tariffs.json` fallback if everything else fails |

Free hosts have an ephemeral disk: anything the in-app updater writes is lost on restart,
but it re-fetches on boot, and the Action-committed `rates_live.json` ships with every deploy.

## First-day checklist
* `GET /api/v1/rates/status` -> `last_success` should be set. If `errors` shows nha.gov.pk failing, see below.
* `POST /api/v1/admin/update-now` (header `X-API-Key: <TOLL_ADMIN_KEY from Render>`) to force a check.