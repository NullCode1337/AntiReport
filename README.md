# AntiReport

Deletes blacklisted messages from whitelisted users on a Discord user account
(`discord.py-self`).
Initial list template was taken from [here](https://github.com/now4real/forbidden-words)

## Requirements

- Python 3.10 or newer
- A Discord user token whose account has Manage Messages in the servers to
  monitor

On a fresh Linux install you may need:

```bash
sudo apt install python3 python3-venv
or
sudo pacman -S python python-virtualenv
```

## Install

```bash
cd /path/to/AntiReport
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Configure

1. Create the environment file:

```bash
cp .env.example .env
chmod 600 .env
```

2. Put the account token in `.env`:

```ini
DISCORD_TOKEN=your_token_here
```

Alternatively, leave `DISCORD_TOKEN` empty and write the token to `token.txt`.
A real environment variable (`export DISCORD_TOKEN=...`) overrides `.env`.

3. List the servers (guilds) to monitor in `whitelist_servers.txt`, one ID per
   line. Messages from any other server are ignored; an empty list monitors
   nothing.

```text
123456789012345678
234567890123456789
```

4. Add the user IDs whose messages should be checked to `whitelist_users.txt`,
   one per line:

```text
345678901234567890
456789012345678901
```

5. Edit `blacklist_words.txt` if needed, one term per line. Other settings in
   `.env` (`MATCH_MODE`, workers, queue size, logging) are documented in
   `.env.example`.

6. Optional: to re-post every deleted message into a channel, create a webhook
   in that channel and add it to `.env`:

   ```ini
   WEBHOOK_ENABLED=true
   WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/your-token
   ```

## Run

```bash
cd /path/to/AntiReport
venv/bin/python main.py
```

A successful start logs:

```text
logged in as <name> (<id>), <n> guild(s)
```

Stop with `Ctrl+C`.

The whitelist, server whitelist, and blacklist files are re-read automatically
when they change; no restart is needed after editing them.

## Run as a service (systemd)

Adjust `User` and paths, then save as
`/etc/systemd/system/antireport.service`:

```ini
[Unit]
Description=AntiReport Discord selfbot
After=network-online.target
Wants=network-online.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/AntiReport
ExecStart=/home/youruser/AntiReport/venv/bin/python /home/youruser/AntiReport/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now antireport
```

Follow the log:

```bash
journalctl -u antireport -f
```

Restart after changing `.env`:

```bash
sudo systemctl restart antireport
```

## Troubleshooting

- `set DISCORD_TOKEN or put the token in token.txt`: no token was found in
  the environment, `.env`, or `token.txt`.
- `login failed: Discord rejected the token`: the token is invalid or expired.
- `missing Manage Messages in channel <id>; skipping it`: the account cannot
  delete in that channel; grant the permission or remove the channel from use.
- `blacklist not found: ...`: check `BLACKLIST_FILE` in `.env`.
- `server whitelist is empty: no servers will be monitored`: add guild IDs to
  `whitelist_servers.txt`.
- `WEBHOOK_ENABLED is true but WEBHOOK_URL is empty` or
  `WEBHOOK_URL does not look like a Discord webhook URL`: fix `WEBHOOK_URL`.
- `webhook rejected further re-posts (status 401/403/404)`: the webhook was
  deleted or its URL is wrong; re-posting stays off until restart.
- Run with `LOG_LEVEL=DEBUG` in `.env` for more detail.
