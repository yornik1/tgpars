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
> missed. For true 24/7 use the VPS + systemd setup below instead, and keep the
> Mac LaunchAgent **unloaded** (one Telegram session = one writer; never run both).

## 24/7 on a Linux VPS (systemd)

Deployed on host `claw` (Ubuntu, AWS). The unit `tgpars-collector.service` runs
`tgpars-collect` from the repo venv with `Restart=always` and starts on boot.

Provision / update:
```bash
# from the repo on the Mac (with the local daemon stopped):
rsync -az --exclude .venv --exclude .git --exclude data ./src ./pyproject.toml claw:~/tgpars/
rsync -az ./.env ./sessions/tgpars.session claw:~/tgpars/        # sensitive: account access
rsync -az ./data/tgpars.db claw:~/tgpars/data/                   # optional: ship history

ssh claw 'cd ~/tgpars && python3 -m venv .venv && .venv/bin/pip install -e ".[analysis]"'
scp deploy/tgpars-collector.service claw:/tmp/
ssh claw 'sudo mv /tmp/tgpars-collector.service /etc/systemd/system/ && \
  sudo systemctl daemon-reload && sudo systemctl enable --now tgpars-collector'
```

Operate:
```bash
ssh claw 'systemctl status tgpars-collector'
ssh claw 'journalctl -u tgpars-collector -f'            # live logs
ssh claw 'sudo systemctl restart tgpars-collector'
```

> Same single-writer rule: before running `tgpars-backfill` on the VPS, stop the
> service (`sudo systemctl stop tgpars-collector`), backfill, then start it again.
