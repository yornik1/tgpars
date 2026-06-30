# tgpars

Collect messages from **closed** Telegram crypto-signal groups (read-only, from
your own account) and detect manipulation: deleted posts, edited-after-the-fact
signals, and fake win-rates.

## How it works

An always-on **collector daemon** (Telethon user-client) snapshots every message
the instant it arrives, so deletions and edits can be detected as a diff against
what we stored — Telegram does not return a message's content once it is deleted.

```
[Telethon daemon] --> [SQLite] --> [exporter] --> Markdown/JSONL
 live listener         messages      (no LLM)          |
 new / edit / delete   edits                           v
                       deletions          in-session LLM agent (Claude Code /
                                          Codex, subscription) classifies signals,
                                          links result-updates by tag, flags lies
                                                        |
                                          later: price verify (ccxt, pure code)
```

> By design the app embeds **no LLM API**. Collection and export are plain
> Python; the "is this signaller lying?" analysis is done by an LLM agent in a
> session reading the exporter output, so there is no per-token API billing.

> The collector is strictly read-only: it never sends messages, reacts, or
> joins/leaves chats. Reading and storing messages from a group your account is
> already in behaves like a normal client; keep one session and modest limits.

## Setup

1. Get API credentials at <https://my.telegram.org> → *API development tools*.
2. Create your env file and fill it in:
   ```bash
   cp .env.example .env
   ```
3. Install:
   ```bash
   python -m venv .venv && .venv/bin/pip install -e .
   ```
4. Log in once and list the chats your account can see (copy ids into `TG_TARGET_CHATS`):
   ```bash
   .venv/bin/python -m tgpars.scripts.login
   ```
5. Run the collector:
   ```bash
   .venv/bin/python -m tgpars.collector.daemon
   ```

## Status

- [x] Phase 0 — project scaffold, config, login/dialog lister
- [x] Phase 1 — collector daemon + SQLite schema (new / edited / deleted)
- [x] Phase 1.5 — read-only history backfill (`tgpars-backfill`)
- [x] Phase 2 — DB exporter for in-session LLM analysis (`tgpars-export`)
- [x] Phase 3 — price verification (`verify.py`, ccxt, pure code)
- [ ] Phase 4 — manipulation metrics + report (winrate, deletion/edit rates)
- [ ] 24/7 hosting (launchd / VPS) so live deletions are not missed
