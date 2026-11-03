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

3. Configure `whitelist.json`. Servers listed in `servers` are the only ones
   monitored; only authors listed in `users` are checked. Both lists are
   required — an empty list matches nothing. A user entry is either a bare ID
   or an object with an optional `webhook` flag (default `false`):

```json
{
  "users": [
    345678901234567890,
    {"id": 456789012345678901, "webhook": true}
  ],
  "servers": [123456789012345678, 234567890123456789]
}
```

   `"webhook": true` opts that user into webhook re-posting (see step 5);
   leave it off if you do not use the webhook.

4. Edit `blacklist_words.txt` if needed, one term per line. Other settings in
   `.env` (`MATCH_MODE`, workers, queue size, logging) are documented in
   `.env.example`.

5. Optional: to re-post deleted messages into a channel, create a webhook in
   that channel and add it to `.env`:

   ```ini
   WEBHOOK_ENABLED=true
   WEBHOOK_URL=https://discord.com/api/webhooks/123456789012345678/your-token
   ```

   Then set `"webhook": true` on the `users` entries whose deleted messages
   should be re-posted:

   ```json
   {
     "users": [
       {"id": 345678901234567890, "webhook": true},
       456789012345678901
     ],
     "servers": [123456789012345678]
   }
   ```

   Only users with the flag are forwarded; with no `true` entry nothing is
   re-posted.

   Image attachments on a forwarded message are downloaded into memory and
   re-uploaded with the webhook post (multipart/form-data), so the deleted
   image is preserved too. Limits default to 4 images per message and 8 MB per
   image; adjust `WEBHOOK_MAX_IMAGES` and `WEBHOOK_MAX_IMAGE_MB` in `.env`.
   Oversized, non-image, or unreachable attachments are skipped and the text is
   still re-posted.

   If the deleted message was a reply, the re-post is prefixed with the
   mentioned user (`<@id>`) so the person it answered is visible in the
   webhook channel. Only that one mention is allowed; everyone, roles, and
   other user pings stay suppressed. Forwarded messages are not replies and
   get no prefix.

   Optionally rewrite words in the re-post with emoji. Create
   `replacements.json` mapping each emoji to one word or an array of words:

   Any Discord emoji form works as the key (`:shortcode:`,
   `<:custom:123456789012345678>`, or a unicode emoji). Matching follows
   `MATCH_MODE`. Only the webhook copy is rewritten; the message on the server
   is never modified, and none of these words are deleted unless they are also
   in `blacklist_words.txt`. The file is optional.

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

`whitelist.json`, `blacklist_words.txt`, and `replacements.json` are read once
at startup with no background file polling. Restart after editing any of them
(`sudo systemctl restart antireport` under systemd).

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
# Optional
Nice=10

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
  the `servers` array in `whitelist.json`.
- `user whitelist is empty: no messages will be checked or deleted`: add user
  IDs to the `users` array in `whitelist.json`.
- `no users have "webhook": true, so nothing will be re-posted`: add
  `"webhook": true` to a user entry in `whitelist.json`.
- `"webhook_users" is ignored`: the old separate list was replaced; put
  `"webhook": true` on the user entries instead.
- `cannot read whitelist ...`: the JSON is malformed; the whitelist is empty.
  Fix the file and restart.
- `WEBHOOK_ENABLED is true but WEBHOOK_URL is empty` or
  `WEBHOOK_URL does not look like a Discord webhook URL`: fix `WEBHOOK_URL`.
- `webhook rejected further re-posts (status 401/403/404)`: the webhook was
  deleted or its URL is wrong; re-posting stays off until restart.
- Run with `LOG_LEVEL=DEBUG` in `.env` for more detail.
