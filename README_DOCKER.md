# Docker deployment — existing paper account

This overlay adds Docker files; it does not replace your trading strategies or
Python services. Based on your complete October 15 upload. No server has been
purchased, no credentials used, no messages sent, and no account moved here.

## 1. Copy the overlay
Copy Dockerfile, compose.yaml, .dockerignore, requirements-docker.txt and this
README to your project root. Merge deploy/ and tests/ into existing folders.
Finder hides .dockerignore: Command+Shift+period shows it.

Install Docker Desktop on your Mac and start it:
https://docs.docker.com/desktop/setup/install/mac-install/
Choose the correct Apple Silicon or Intel download. Confirm your employer's
Docker Desktop license requirements. Linux servers use Docker Engine instead.

In your project terminal (shell %, not Python >>>):

    docker version
    docker compose version
    docker compose build

Build while the current trader runs; building cannot trade. This image contains
only allowlisted application files, not secrets, account data, backtest results,
config JSONs or your Mac virtual environment. Python 3.14 is used. Scientific
packages are installed from your existing requirements; versions remain unpinned
as in your upload. /opt/build-requirements.txt records the resolved versions.
After the first successful build, retain the image and that manifest for your
formal evaluation. A future rebuild can resolve different dependencies.

Build has NOT been run by the assistant: Docker is unavailable in its environment.
Treat the following local build/tests as a required deployment gate. If a wheel
is unavailable for your platform/Python version, share the build error; do not
silently switch interpreter versions mid-evaluation.

## 2. Export the existing account (stop all four processes first)
Stop trader, notifier, reporter and advisor with Ctrl+C; wait for each shell
prompt. Keep the original runtime directory as your pre-move recovery copy.
Do not restart the original after activating the exported account.

With your virtual environment active:

    python -m unittest discover -s tests -p test_deployment.py -v
    python deploy/export_account.py --source runtime/paper.sqlite --destination docker-runtime

The destination must NOT already exist. If you previously created it, inspect it
and choose a fresh destination; do not delete an account to get past this guard.
The utility holds all four worker locks, uses SQLite's backup API, checks integrity,
preserves the source, copies the three databases that exist, and rewrites ONLY the
copied report/advisor source-path fields to /app/runtime/paper.sqlite. It preserves
report IDs, recipients, attempts, delivery states, fills, holdings and stops.
A missing sidecar is allowed if that feature has not yet been used. The configuration
cache is copied; old preview files/logs stay in the original runtime folder.
Migration metadata and hashes are recorded in migration.json. EXPORT_FAILED means
an incomplete destination: do not deploy it. A worker using --send-test does not
hold the normal reporter lock: finish all one-off tests before exporting too.

Do not use cp on a running SQLite database. Do not copy databases separately while
workers are writing. Restart gaps are recorded by existing outage handling; no
historical fills are invented during deployment downtime.

## 3. Local credentials and permissions
Use your existing root .env and secrets/google-sheets-service-account.json.
Compose reads .env to inject each service's own credentials. .env is NOT mounted
into containers and never copied into the image. Never share `docker compose
config` output: it expands credentials. Use `docker compose config --quiet` instead.

Required .env entries:
GOOGLE_SHEETS_ID, GOOGLE_SHEETS_RANGE (optional; Config!A:B),
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GMAIL_SENDER, GMAIL_APP_PASSWORD,
REPORT_RECIPIENT, GEMINI_API_KEY.

Keep GOOGLE_APPLICATION_CREDENTIALS for native Python tools. Compose overrides
that path for the trader to /run/secrets/sheets_key using the same local JSON file.
Gemini model in advisor.json should remain gemini-3.1-flash-lite (your working model).

For local Docker Desktop permissions, append to .env the numeric values printed
by `id -u` and `id -g`, e.g. APP_UID=501 and APP_GID=20. Use YOUR actual values.
This runs containers as your host user, not root. The server instructions below
use the server user's IDs instead. Keep config/advisor/reporting JSON files in
project root; they are mounted read-only. docker-runtime must be writable by the
configured user; the Sheets key must be readable by that user.

    docker compose config --quiet
    docker compose run --rm --no-deps trader python deploy/preflight.py
    docker compose run --rm --no-deps trader python -m unittest discover -s tests -p test_advisor.py -v
    docker compose run --rm --no-deps trader python -m unittest discover -s tests -p test_weekly_reports.py -v

Other existing tests may need their own fixture/import maintenance. In particular,
this overlay does not claim to fix unrelated legacy tests.

## 4. Start one copy of the account

    docker compose up -d --build
    docker compose ps
    docker compose logs --tail=100 -f

This activates paper trading AND real Telegram/Gmail delivery using configured
recipients. Outstanding queued notifications may send, and the reporter may send
a due report. Do not activate a duplicate account on a second machine.

The preflight refuses an empty runtime instead of quietly resetting your account.
Notifier/reporter/advisor wait for a recent quote healthcheck from the trader.
No inbound application ports are exposed. Only the trader receives the Sheets key;
each other service receives only its own credential variables.

Ctrl+C exits the log viewer, not detached containers. To stop ALL workers:

    docker compose stop

To inspect one service:

    docker compose logs --tail=100 trader
    docker compose logs --tail=100 advisor
    docker compose exec advisor python advisor.py --db /app/runtime/paper.sqlite --show

To test delivery deliberately (these send real test messages):

    docker compose exec notifier python telegram_setup.py --test
    docker compose exec reporter python weekly_reports.py --db /app/runtime/paper.sqlite --send-test

To test Gemini with synthetic data (consumes quota):

    docker compose exec advisor python test_gemini_connection.py

Restart a worker, then verify continuity:

    docker compose restart trader

Check restored balances, trade IDs, no repeated fills, quote freshness, and the next
completed-candle decision. Docker on your Mac still needs the laptop awake.

Healthcheck means quote age <=120 seconds, NOT that the full trading system works.
Inspect hourly close_decision/late_forecast_skipped logs and advisor/delivery status
as well. Docker does not automatically restart a process just because it is marked
unhealthy. External health alerts and an automated backup schedule remain separate
operational work. Restart policies only recover exited processes.

## 5. Suggested server: AWS Lightsail Linux
Start by assessing the Linux/public IPv4 plan with 2 vCPUs, 4 GB RAM, 80 GB disk,
listed at US$24/month on 2026-10-15, before tax, backups and overages. This is a
starting sizing estimate, NOT a benchmark; verify GARCH remains under your five-
minute decision deadline and monitor memory/CPU burst capacity. No GPU is needed.

Why not DigitalOcean for this exact version? Droplets block outbound SMTP ports
25, 465 and 587. Your Gmail sender uses 465. DigitalOcean would require changing
email transport, such as implementing Gmail's HTTPS API with OAuth.

Use an employer-approved AWS account, choose a suitable region, Linux/Unix OS-only
Ubuntu 24.04 LTS (x86_64), an SSH key, and restrict inbound SSH to your IP. No web
ports are needed. Enable billing alerts and plan separate backups. Verify access
to Coinbase from the chosen region and employer data-location requirements.

Install Docker Engine + Compose using the official Ubuntu instructions:
https://docs.docker.com/engine/install/ubuntu/
Enable Docker on boot. Run the official hello-world check. Docker group membership
grants powerful host access; use only the deployment admin account.

Copy clean code using your employer's private Git repository or SCP/rsync over SSH.
Do not upload .env, secrets or runtime to a public repository. Rebuild the image
ON the x86_64 server; an Apple Silicon-built image may use a different architecture.

Before final cutover, test outbound Gmail TLS without credentials on the server:

    python3 -c "import smtplib,ssl; s=smtplib.SMTP_SSL('smtp.gmail.com',465,timeout=15,context=ssl.create_default_context()); print('Gmail TLS reachable'); s.close()"

This checks connectivity, not account authentication/delivery. No email is sent.

## 6. Final transfer to the server
Build the code image on the server before cutting over. Then stop ALL local
containers with `docker compose stop` (or all native workers if never started Docker).

If using local Docker, export the now-current account, NOT the old native copy:

    python deploy/export_account.py --source docker-runtime/paper.sqlite --expected-source /app/runtime/paper.sqlite --destination server-runtime

If still using native Python, use --source runtime/paper.sqlite instead.
The exporter verifies ledger paths against the source. For Docker-origin exports,
the ledger identifies /app/runtime/paper.sqlite rather than the Mac path: use the
--expected-source option described below.

Transfer server-runtime to the server project folder as docker-runtime, and send
.env and secrets separately over SSH. Use an EMPTY destination server account
folder; never merge two active account histories. Replace APP_UID/APP_GID in the
server .env with that server admin's `id -u` / `id -g`. Make files readable/writable
by that user; keep .env and key permissions restricted (chmod 600).

On the server run the preflight and `docker compose up -d`. Verify account balances,
new quote timestamps, a new candle decision, and delivery/advisor status. Then test
an orderly server reboot. Ensure all four containers return. Only after this check
should you rely on the server with your laptop closed.

Do not resume the laptop's stale account if the server has processed trades. A
rollback requires stopping the server and exporting its latest state back, with
--target set to the actual receiving database path. Preserve all three ledgers.

## 7. Backups and updating
For a consistent manual backup: stop all four containers, export the current
account to a new dated folder using --expected-source /app/runtime/paper.sqlite,
then start containers again. Store a protected copy off-server and test restoration.
An account export is sensitive financial/operational data even without API keys.
It is not a backup of code or credentials; protect those separately.

    python deploy/export_account.py --source docker-runtime/paper.sqlite --expected-source /app/runtime/paper.sqlite --destination backups/account-YYYYMMDD-HHMMSS
    docker compose up -d

Use unique backup names. Keep source paths /app/runtime/paper.sqlite for Docker
restores. Do not delete runtime to fix errors. Existing reports in sending/running
states are handled by your existing uncertain/interrupted recovery logic.

For code updates: back up, then `docker compose up -d --build`. For credential
changes: `docker compose up -d --force-recreate`; a simple restart does not reload
Compose environment values. For advisor/report JSON changes, restart those workers.
Trading configuration remains governed by your existing Sheets validation.

## Evidence and limits
Export helper tests check source preservation, copied account values, ledger IDs,
refusal to overwrite destinations, worker locks, and foreign-ledger rejection.
No cloud deployment or real API/send test was performed by the assistant.
No Docker daemon is available there, so a real image build is still required.

References checked 2026-10-15:
https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html
https://docs.digitalocean.com/support/why-is-smtp-blocked/
https://docs.docker.com/compose/how-tos/startup-order/
https://docs.docker.com/reference/compose-file/services/
