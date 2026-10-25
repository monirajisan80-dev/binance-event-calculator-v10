# Binance Event Tracker Bot

## Setup
Supported runtime: Python 3.11 (Docker uses the bundled Playwright runtime); the pinned Playwright 1.47 dependencies do not install cleanly on Python 3.13.
1. Create a bot with @BotFather → `BOT_TOKEN`. Get the owner's numeric user ID from @userinfobot → required `OWNER_ID`. Do not use a bot ID or channel ID.
2. Get `TG_API_ID` / `TG_API_HASH` at https://my.telegram.org (needed to read the Binance channel).
3. Locally: `pip install telethon && python gen_session.py` → copy `TG_SESSION_STRING`.
4. Local run: `pip install -r requirements.txt && playwright install chromium && cp .env.example .env && python main.py`

## Deploy on Render
- Push this folder to GitHub → Render → New → Blueprint (uses `render.yaml`, Docker, Background Worker + 1 GB disk for SQLite).
- Fill in the secret env vars in the dashboard.
- Note: the free plan has no persistent disk and sleeps; use Starter or above.

## Usage
- `/start` – live events as buttons → details, pairs, pool, reward per $1,000.
- `/admin` – manage channels and keywords, add event by link, edit/delete events, run scrape now.
- `/cancel` – discard a pending admin input.

## Owner and regular admins
- The configured `OWNER_ID` is the Super Admin. The owner always has access and cannot be removed from inside Telegram.
- Owner: `/admin` → **Manage admins** → **Appoint admin** → send a positive numeric Telegram user ID. The user must start the bot to receive messages.
- Owner: **Manage admins** → **Revoke [ID]** → **Revoke access**. Revocation takes effect on the next action, including already-open buttons and pending text prompts. The owner is the only person allowed to appoint or revoke admins.
- Regular admins retain event/channel/keyword/scrape controls but cannot manage admins, even with a forged callback.
- Admin appointments live in SQLite and survive restarts when the persistent disk is retained. Announcement notifications use the current owner/admin list.
- Regular admins are never assigned from configuration files or environment variables. `ADMIN_IDS` is no longer read or imported, even if left over from an older deployment. No server changes or restarts are needed to appoint or revoke an admin.
- Upgrade: keep `OWNER_ID` set to the primary owner's Telegram ID; this is the only role configured during installation and establishes who may open the owner controls. Back up the persistent database before upgrading. Existing events and saved admin appointments are retained; the owner can review and revoke any existing admin through **Manage admins**. New installations start with no regular admins.

## Dynamic channels and keywords
- Owner and appointed admins: `/admin` → **Channels** → **Add channel** → send an `@username`, `https://t.me/username` URL, or a `-100…` channel ID. Tap a channel's remove button to stop matching its messages.
- `/admin` → **Keywords** → **Add keyword**; tap a keyword to remove it. Keywords match case-insensitively and case-only duplicates are rejected. Empty lists intentionally pause channel monitoring or keyword matching.
- Lists persist in PostgreSQL. `DEFAULT_CHANNELS` (comma-separated) and `DEFAULT_KEYWORDS` are used only for first-time setup. The legacy `DEFAULT_CHANNEL` is still accepted as an initial default. Editing environment defaults does not overwrite saved lists, even when all entries have been removed.
- Existing single-channel and keyword settings migrate automatically. Stable item IDs make old removal buttons safe after other list changes.
- The Telethon monitoring account subscribes to added public channels automatically, normally within 15 seconds; Telegram rate limits or access restrictions can delay this. Private numeric IDs require the account to already have access. Invite links are not accepted. Subscription failures notify the owner/admins and retry automatically. Removing a target stops processing immediately on subsequent messages; it does not unsubscribe your personal account.

## Per-event admin setup
Binance announcements vary in format, so after an event is created open it and use **Edit**:
- **Edit title** – `/admin` → **Manage events** → select an active/live event → **Edit event** → **Edit title** → send the new title. Both owner and admins can do this. Titles must be nonblank and at most 200 characters. Upcoming and closed events cannot be renamed, and the event must still be live when the title is submitted.
- Custom titles appear in event lists and details immediately and survive restart and repeat announcement ingestion; they do not change the announcement URL, dates, pool, or leaderboard estimates. HTML characters in titles are escaped safely.
- **Remaining pool** – the pool shared by rank 1001+ (e.g. `80`). Required for the estimate.
- **Leaderboard URL** – auto-detected when possible; set it if missing.
- **Fixed slots** – e.g. `1-1 10`, `6-20 15 split`.

## Math
`V_total = Σ volume(rank > 1000)`, `R/$ = pool / V_total`, `R/$1000 = R/$ × 1000`, per-user reward is capped at the event's extracted reward cap.

## Event-specific reward caps
- Announcements are scanned for explicit per-user/participant/account maximums (for example, “Each user can receive up to 0.1 BNB” or “Maximum reward of 25 USDT per participant”). Caps and their tokens are stored with each event and used during leaderboard refresh.
- A cap must use the same token as the prize pool. Conflicting caps, unmatched currencies, and unrecognized rule wording are treated as unparsed; no exchange-rate conversion is guessed. Multi-tier announcements still need rule review.
- When extraction is unavailable, `FALLBACK_REWARD_CAP` applies only to `FALLBACK_REWARD_TOKEN` (defaults: 0.05 BNB). The legacy `USER_CAP_BNB` is accepted only as a fallback amount. Event details clearly label “announcement”, “fallback”, or unavailable. With no matching cap/fallback token, the capped leaderboard estimate is withheld rather than using BNB as another currency.
- Re-ingestion updates the scraped cap, marks old estimates stale, and retains complete counts while preserving custom event titles. Upgrade migration also attempts extraction from rules already saved in the database.

## Database and upgrades
All state (events, admin roles, monitoring lists) lives in **Supabase PostgreSQL** via `asyncpg`. Set `DATABASE_URL` to the URI from Supabase → Project Settings → Database → Connection string. On Render use the **Session pooler** URI (port 5432, IPv4-compatible); the Transaction pooler (6543) also works because prepared-statement caching is disabled. Tables are created and migrated automatically on startup (guarded by an advisory lock). `DB_POOL_MIN`/`DB_POOL_MAX` tune the connection pool (defaults 1/5).

### Deploying on Render
- `render.yaml` defines an always-on **Background Worker** (Docker). No port is bound and no disk is needed.
- Set `BOT_TOKEN`, `OWNER_ID`, `DATABASE_URL`, `TG_API_ID`, `TG_API_HASH`, `TG_SESSION_STRING` in the Render dashboard.
- To run as a **Web Service** instead, Render injects `PORT` and the bot serves `/` and `/healthz` (returns 503 if the database is unreachable). Set the health check path to `/healthz`. Run only one instance: Telegram polling allows a single consumer.

## Reliability
asyncio throughout, single shared Chromium (auto-relaunch), semaphore + delays for rate limiting,
a lock + `max_instances=1` so scrape cycles never overlap, per-request timeouts, all jobs/handlers wrapped
so errors are logged not fatal, hourly purge of events closed > 30 days.

Tests: `TEST_DATABASE_URL=postgresql://... pytest` — point it at a **disposable** PostgreSQL database (tables are dropped between tests). Without it, database tests are skipped. No Telegram login or Binance access is needed.

Live deployment still requires your Telegram credentials. Local automated tests do not verify Telegram delivery or Binance page compatibility.


## v7 — complete pagination and participant counts
- No 200-page cap, 150-second scraping budget, three-stagnant-page cutoff, or 240-second job timeout remains. Long runs continue until a disabled final-page control or verified API pagination metadata indicates completion. The next scheduled cycle waits/skips while the previous cycle is still running; updates can take longer than five minutes on large leaderboards.
- Pagination waits for a parsed page response before clicking again. A slow response does not trigger another click. Empty pages with explicit advancing page metadata are traversed. Reported page numbers must advance by exactly one; repeated pages, skipped pages, missing controls before the reported final page, and ranked-total mismatches fail the refresh.
- Individual navigation/click/response waits still have bounded timeouts. An error or cancellation closes the browser context and does not publish partial totals; the previous successful stats and timestamp remain. Announcement re-ingestion now retains complete snapshots and marks estimates stale until replacement succeeds.
- **Total Participants** is the explicit source-provided joined/participant count, never inferred from generic `total`, the highest rank, or the number of captured users. Supported JSON fields: `totalParticipants`, `participantCount`, `joinedCount`, `totalJoined`, `totalParticipantCount`. If the source does not provide a recognized joined count, the card says **unavailable from source**.
- **Displayed Participants (Ranked)** counts distinct captured rank+valid-volume rows across ALL leaderboard pages, including ranks 1–1000. The separate **Participants (Rank 1001+)** metric and volume/reward calculations remain limited to ranks greater than `SKIP_TOP` (default 1000). This assumes ranks uniquely identify leaderboard participants.
- Stats remain in existing SQLite `last_stats` JSON; no database reset or schema migration is needed. Old snapshots show ranked counts as pending until the next complete refresh.

### Verification and remaining live check
- 103 automated tests passed, including real headless Chromium over 205 sequential local HTTP pages (2,050 ranked users), delayed responses, multiple empty pages, invalid rows, skipped-page rejection, cancelled-run cleanup, and SQLite persistence of distinct joined/ranked/tail counts.
- On 2026-10-25 UTC a real browser scrape was attempted against the publicly advertised URL `https://www.binance.com/activity/trading-competition/202606tradersleague3/futures-overall-leaderboard`. Binance redirected to its English page and displayed a 404-style “We're sorry, but something went wrong” screen, with HTTP 202 and no leaderboard/next controls. The scraper correctly rejected the run and closed all contexts. This is NOT a successful live pagination verification.
- Full live compatibility and Telegram delivery remain unverified. Supply a currently working event-specific leaderboard URL, then run **Run scrape now** from the deployed bot. Confirm the source's joined count, ranked count, final page, and sum of all rank-1001+ volumes. Some events expose only the top N users: the bot cannot obtain participants Binance does not make accessible and must not label those results as all joined users.
- Website-specific field names, pagination selectors and source totals must be verified against that working event before calling this production-ready. No API endpoints, credentials or access restrictions are bypassed.


## v8 — fresh audit and corrections (2026-10-25)
- Re-ingestion no longer clears the previous complete snapshot. Settings-version tracking with conditional SQL publication prevents pool/link changes during scraping from publishing obsolete estimates. Per-event refreshes are serialized; stale snapshots retain their counts but withhold reward estimates.
- Startup cleanup is registered before resource acquisition and unwinds services independently in reverse order. Health listeners close, and SIGTERM/SIGINT request graceful shutdown.
- Ranked responses bind to the first recognized response request identity (endpoint, non-pagination parameters and request body); other request identities cannot modify the snapshot. A second identity check after JSON parsing closes an asynchronous response race. This is not a universal Binance endpoint detector: the first matching endpoint and tab still require live-event validation.
- Malformed ranked entries abort collection instead of silently reducing totals. Missing/disabled Next without final-page metadata is now ambiguous and rejected. If metadata says pages remain, temporary disabled controls are awaited. No page-count or overall-run cutoff is added.
- Multiline event periods retain both dates. Non-reward trading/deposit/purchase limits are not accepted as reward caps. Announcement and embedded monitoring links must be valid HTTPS Binance addresses, not substring imitations.

### Fresh verification
- Complete suite: **129 passed, 0 skipped** on Python 3.13 with the sandbox's Playwright 1.56 browser and installed dependencies. Includes 205-page collection and regression reproductions for snapshot loss, stale writes, serialized refreshes, startup failures, cleanup failures, listener rebinding, unrelated JSON races, malformed terminal rows and ambiguous Next controls.
- Exact requirements on Python 3.11: **100 non-browser tests passed**. An earlier full exact-dependency run passed 112 tests and skipped 12 browser cases. Playwright 1.47 Chromium downloaded successfully but cannot launch in this sandbox because its system libglib library is unavailable; the final exact-version browser/Docker run remains unverified. The complete 129-test browser run used the sandbox's preinstalled newer browser, not the Docker image.
- Fresh live attempt at the previously recorded Binance URL again failed to receive a verifiable leaderboard page; `IncompleteLeaderboard` was raised and zero browser contexts remained. No partial snapshot was published. A current accessible event URL and deployed Telegram account are still required for full live verification.
- No claim of universal production readiness or absence of all memory leaks is made: these are bounded automated tests, not a long-duration production soak or successful live Telegram/Binance run.
