# tgpars

Collect messages from **closed** Telegram crypto-signal groups (read-only, from
your own account) and detect manipulation: deleted posts, edited-after-the-fact
signals, and fake win-rates.

## How it works

An always-on **collector daemon** (Telethon user-client) snapshots every message
the instant it arrives, so deletions and edits can be detected as a diff against
what we stored — Telegram does not return a message's content once it is deleted.

```
[Telethon daemon] --> [SQLite]  (messages / edits / deletion_events)
 live listener            ^
 new / edit / delete      |
                    later phases: signal parser (LLM) -> price verify (ccxt) -> report
```

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
- [ ] Phase 2 — LLM signal parser
- [ ] Phase 3 — price verification (ccxt)
- [ ] Phase 4 — manipulation metrics + report
