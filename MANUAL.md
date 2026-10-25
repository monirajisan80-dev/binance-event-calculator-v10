# Binance Event Bot — User Manual

The same guide is available inside the bot with `/help` or the **📖 Help** button.
What you see depends on your role; the bot checks your role on every tap.

## Roles at a glance

| Feature | User | Admin | Owner |
|---|:-:|:-:|:-:|
| View live events, metrics, refresh, help | ✅ | ✅ | ✅ |
| Admin panel (`/admin`) | — | ✅ | ✅ |
| Add event by link, edit / delete events | — | ✅ | ✅ |
| Run scrape now | — | ✅ | ✅ |
| Channels & keywords | — | — | ✅ |
| Appoint / revoke admins | — | — | ✅ |

The owner is the Telegram account in `OWNER_ID`. Admins are appointed by the owner inside the bot.
Hidden buttons are also blocked on the server, so an old or forged button press is refused.

## Commands
- `/start` — list live events
- `/help` — this manual (tailored to your role)
- `/admin` — admin panel (admins and owner)
- `/cancel` — cancel a pending input

## Understanding an event
- **Status** — Upcoming, Live (time left) or Closed.
- **Total Volume (Rank 1001+)** — combined volume of everyone ranked 1001 and below.
- **Total Participants** — all entrants reported by Binance. **Displayed Participants** — those listed on the leaderboard.
- **Prize pool** — the full prize. **Remaining pool** — the part shared among rank 1001+.
- **Per $1,000 volume** — estimated reward for each $1,000 you trade.
- **Per-user cap** — maximum one person can win; the bot shows the volume at which you hit it.
- **Fixed rewards** — set prizes for specific rank ranges.
- **Updated** — time of the last leaderboard scrape. Estimates refresh automatically.

## Admin guide — setting up an event
1. Open `/admin` → **➕ Add event by link** and paste the Binance announcement link.
2. Open the event (**📋 Manage events**) → **⚙️ Edit event**.
3. Set **Remaining pool** and **Leaderboard URL** — both are needed for the per-$1,000 estimate.
4. Optionally set **Total pool**, **Fixed slots** and (live events only) **Title**.
   Fixed slots, one per line: `1-1 10`, `2-5 3`, `6-20 15 split` (`split` = shared equally).
5. **▶️ Run scrape now** refreshes every event immediately. **🗑 Delete** removes an event.

## Owner guide
- **📡 Channels** — Telegram channels watched for new event posts (`@name`, `t.me/name` or `-100…` ID). Tap an entry to remove it.
- **🔑 Keywords** — words that mark a channel post as a new event.
- **👥 Manage admins** — appoint by numeric Telegram user ID, or revoke. The owner cannot be removed.
