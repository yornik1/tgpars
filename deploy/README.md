# Deploy — 24/7 collector (macOS launchd)

The collector must run continuously: deletions/edits are only observable while
it is connected. On macOS this is done with a per-user LaunchAgent.

`com.tgpars.collector.plist` runs `tgpars-collect` from the repo venv, restarts
it on crash (`KeepAlive`), starts it at login (`RunAtLoad`), and logs to
`data/logs/`. Paths in the plist are absolute and machine-specific — edit them
if the repo or venv moves.

## Install / start

```bash
cp deploy/com.tgpars.collector.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.tgpars.collector.plist
launchctl list | grep tgpars        # PID + status (0 = running)
tail -f data/logs/collector.err.log # live logs (telethon logs to stderr)
```

## Stop / restart

```bash
launchctl unload -w ~/Library/LaunchAgents/com.tgpars.collector.plist   # stop
launchctl load   -w ~/Library/LaunchAgents/com.tgpars.collector.plist   # start
```

## ⚠️ Backfilling while the agent runs

The daemon and `tgpars-backfill` share one Telethon session file (single
writer). **Unload the agent before backfilling**, then load it again:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.tgpars.collector.plist
.venv/bin/python -m tgpars.scripts.backfill 1000
launchctl load   -w ~/Library/LaunchAgents/com.tgpars.collector.plist
```

> A Mac that is asleep does not run the agent — live deletions during sleep are
> missed. For true 24/7 use an always-on host (small VPS) with the same setup
> under systemd instead.
