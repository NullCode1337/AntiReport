# AntiReport

Deletes blacklisted messages from whitelisted users on a Discord user account
(`discord.py-self`). Initial word list taken from
[now4real/forbidden-words](https://github.com/now4real/forbidden-words).

## Requirements

- Python 3.10 or newer
- A Discord user token whose account has Manage Messages in the servers to
  monitor

On a fresh Linux install you may need `python3` and `python3-venv`.

## Install

```bash
cd /path/to/AntiReport
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Configure

Copy the environment template and lock it down:

```bash
cp .env.example .env
chmod 600 .env
```

Put the token in `.env`, or leave it empty and write the token to `token.txt`.
A real environment variable overrides `.env`.

```ini
DISCORD_TOKEN=your_token_here
```

`whitelist.json` needs two ID arrays. `servers` limits which servers are
monitored, `users` limits who is checked, and an empty list matches nothing. A
user entry is a bare ID or an object with an optional `"webhook": true` flag.

```json
{
  "users": [
    345678901234567890,
    {"id": 456789012345678901, "webhook": true}
  ],
  "servers": [123456789012345678, 234567890123456789]
}
```

`blacklist_words.txt` holds one term per line. `MATCH_MODE`, worker counts, and
logging are documented in `.env.example`. All files are read once at startup,
so restart after editing them.

To re-post deleted messages, create a webhook in the target channel and enable
it in `.env`. Only users flagged `"webhook": true` are forwarded.

```ini
WEBHOOK_ENABLED=true
WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/your-token
```

Images are re-uploaded too, capped by `WEBHOOK_MAX_IMAGES` (4) and
`WEBHOOK_MAX_IMAGE_MB` (8). Replies are re-posted with the answered user
mentioned; other pings stay suppressed.

While `WEBHOOK_ENABLED` is true, `...fw` acts as a manual forward. It does
nothing when the webhook is disabled: no message is deleted and nothing is
re-posted. The reply form works from the account itself and from users in
`whitelist.json` (in monitored servers only).

Create `replacements.json` to rewrite words in the re-post only; keys are emoji
and values are a word or array of words. The server copy is never modified.

## Run

```bash
cd /path/to/AntiReport
venv/bin/python main.py
```

A successful start logs `logged in as <name> (<id>), <n> guild(s)`. Stop with
`Ctrl+C`.

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
Nice=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now antireport
journalctl -u antireport -f
```

Restart with `sudo systemctl restart antireport` after changing `.env` or the
JSON/text inputs.
