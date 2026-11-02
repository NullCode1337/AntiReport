"""Discord selfbot that deletes blacklisted messages from whitelisted users.

Runs on a user account (discord.py-self). Every guild message is checked
against whitelist_users.txt; if the author is whitelisted, the message text
(plus embed text and attachment filenames) is scanned with the Aho-Corasick
matcher from blacklist.py and the message is deleted on a hit.

Configuration comes from the real environment or from a `.env` file next to
this script (real environment wins). Variables:

    DISCORD_TOKEN       user token, required unless token.txt exists
    BLACKLIST_FILE      default: blacklist_words.txt
    WHITELIST_FILE      default: whitelist_users.txt
    SERVER_WHITELIST_FILE
                        default: whitelist_servers.txt; only these guilds are
                        monitored, and empty means none
    MATCH_MODE          word (default) | substring
    DELETE_WORKERS      concurrent delete workers, default 2
    DELETE_QUEUE_SIZE   pending deletes before shedding, default 512
    AUTHOR_INDEX_SIZE   messages tracked for edit attribution, default 2048
    WEBHOOK_ENABLED     re-post deleted messages to a webhook, default false
    WEBHOOK_URL         webhook that receives the deleted messages
    WEBHOOK_USERS_FILE  default: webhook_users.txt; only these user IDs are
                        re-posted, and empty means nobody
    LOG_FILE            optional rotating log path (stdout always logs)
    LOG_LEVEL           default INFO

Relative paths in BLACKLIST_FILE/WHITELIST_FILE resolve against this script's
directory, not the process working directory.

Whitelist format: one ID per line, or comma/space separated; '#' starts a
comment. whitelist_users.txt holds user IDs, whitelist_servers.txt holds guild
IDs; only listed guilds are monitored. Both files, and the blacklist, are
re-read when their mtime changes, so entries can be updated without a restart.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import signal
import sys
from collections import OrderedDict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NamedTuple

import discord

from blacklist import Blacklist

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from *path* without overriding the real environment.

    Accepts blank lines, ``#`` comments, an optional ``export`` prefix, and
    single- or double-quoted values. A missing file is not an error.
    """
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError:
        return

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        key = key.strip()
        if not sep or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
            if line[-1] == '"':
                value = value.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
        else:
            comment = value.find(' #')
            if comment != -1:
                value = value[:comment].rstrip()
        os.environ[key] = value


def _env_path(name: str, default: Path) -> Path:
    """Read a path variable, resolving relative entries against BASE_DIR."""
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


_load_env_file(BASE_DIR / '.env')
TOKEN_ENV = 'DISCORD_TOKEN'
TOKEN_FILE = BASE_DIR / 'token.txt'
BLACKLIST_FILE = _env_path('BLACKLIST_FILE', BASE_DIR / 'blacklist_words.txt')
WHITELIST_FILE = _env_path('WHITELIST_FILE', BASE_DIR / 'whitelist_users.txt')
SERVER_WHITELIST_FILE = _env_path('SERVER_WHITELIST_FILE', BASE_DIR / 'whitelist_servers.txt')
MATCH_MODE = os.getenv('MATCH_MODE', 'word').strip().lower()
DELETE_WORKERS = max(1, int(os.getenv('DELETE_WORKERS', '2')))
DELETE_QUEUE_SIZE = max(16, int(os.getenv('DELETE_QUEUE_SIZE', '512')))
WEBHOOK_ENABLED = _env_bool('WEBHOOK_ENABLED', False)
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '').strip()
WEBHOOK_USERS_FILE = _env_path('WEBHOOK_USERS_FILE', BASE_DIR / 'webhook_users.txt')
LOG_FILE = os.getenv('LOG_FILE')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

# Same shape the library accepts in Webhook.from_url; checked at startup so a
# typo fails before the account connects.
WEBHOOK_URL_RE = re.compile(r'discord(?:app)?\.com/api/webhooks/\d{17,20}/[A-Za-z0-9.\-_]{60,}')

# Re-posts waiting behind webhook rate limits. Bounded; a full queue sheds.
FORWARD_QUEUE_SIZE = 256

# Bounded author index for edit detection: message id -> author id. Raw
# MESSAGE_UPDATE payloads do not carry the author, so an edit can only be
# attributed if the original message was seen while it was still recent.
AUTHOR_INDEX_SIZE = max(256, int(os.getenv('AUTHOR_INDEX_SIZE', '2048')))

log = logging.getLogger('antireport')


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', '%Y-%m-%d %H:%M:%S')

    # Root handlers: our own logger and anything the library emits both land
    # in the same format instead of falling through to lastResort.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers[0].setFormatter(formatter)

    if LOG_FILE:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers)

    # Keep the library quiet unless debugging; its per-event logging is
    # heavy on a low-memory host.
    logging.getLogger('discord').setLevel(max(level, logging.WARNING))
    logging.getLogger('discord.http').setLevel(max(level, logging.WARNING))


class IdWhitelist:
    """Set of IDs backed by a file, reloaded when the file changes."""

    def __init__(self, path: Path, label: str = 'whitelist') -> None:
        self.path = path
        self.label = label
        self.ids: frozenset[int] = frozenset()
        self._mtime: int | None = None
        self.reload(force=True)

    def _stat_mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def reload(self, *, force: bool = False) -> bool:
        mtime = self._stat_mtime()
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        if mtime is None:
            if self.ids:
                log.warning('%s file %s disappeared, keeping previous entries',
                            self.label, self.path)
            return False

        ids: set[int] = set()
        try:
            with self.path.open('r', encoding='utf-8', errors='replace') as handle:
                for lineno, line in enumerate(handle, 1):
                    line = line.split('#', 1)[0]
                    for token in line.replace(',', ' ').split():
                        if not token.isdigit():
                            log.warning('%s %s:%d: ignoring non-numeric entry %r',
                                        self.label, self.path.name, lineno, token)
                            continue
                        ids.add(int(token))
        except OSError as exc:
            log.error('cannot read %s %s: %s', self.label, self.path, exc)
            return False

        changed = ids != set(self.ids)
        self.ids = frozenset(ids)
        if changed:
            log.info('%s loaded: %d entries', self.label, len(ids))
        return changed


class PendingForward(NamedTuple):
    """Message data captured before deletion for the webhook re-post."""

    content: str
    author_name: str | None
    avatar_url: str | None


class AntiReportClient(discord.Client):
    def __init__(self, blacklist: Blacklist, whitelist: IdWhitelist,
                 server_whitelist: IdWhitelist, webhook_users: IdWhitelist, *,
                 blacklist_path: Path | None = None, match_mode: str = 'word',
                 **options) -> None:
        super().__init__(**options)
        self.blacklist = blacklist
        self.whitelist = whitelist
        self.server_whitelist = server_whitelist
        self.webhook_users = webhook_users
        self._blacklist_path = blacklist_path
        self._match_mode = match_mode
        self._blacklist_mtime = self._stat_blacklist_mtime()
        self._delete_queue: asyncio.Queue = asyncio.Queue(maxsize=DELETE_QUEUE_SIZE)
        self._workers: list[asyncio.Task] = []
        self._author_index: OrderedDict[int, int] = OrderedDict()
        self._forbidden_channels: set[int] = set()
        self._dropped = 0
        self._forward_queue: asyncio.Queue = asyncio.Queue(maxsize=FORWARD_QUEUE_SIZE)
        self._forward_dropped = 0
        self._webhook: discord.Webhook | None = None
        self._webhook_broken = False

    # -- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self._workers = [
            asyncio.create_task(self._delete_worker(i), name=f'delete-worker-{i}')
            for i in range(DELETE_WORKERS)
        ]
        if WEBHOOK_ENABLED:
            self._workers.append(
                asyncio.create_task(self._forward_worker(), name='forward-worker')
            )
        asyncio.create_task(self._watch_files(), name='file-watcher')

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.close()))
            except (NotImplementedError, RuntimeError):
                pass

    async def _watch_files(self) -> None:
        """Pick up whitelist/blacklist edits without a restart."""
        while not self.is_closed():
            await asyncio.sleep(30)
            try:
                if self.whitelist.reload():
                    log.info('whitelist reloaded (%d entries)', len(self.whitelist.ids))
                if self.server_whitelist.reload():
                    log.info('server whitelist reloaded (%d entries)',
                             len(self.server_whitelist.ids))
                if WEBHOOK_ENABLED and self.webhook_users.reload():
                    log.info('webhook user list reloaded (%d entries)',
                             len(self.webhook_users.ids))
                await self._reload_blacklist()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('file watcher error')

    def _stat_blacklist_mtime(self) -> int | None:
        if self._blacklist_path is None:
            return None
        try:
            return self._blacklist_path.stat().st_mtime_ns
        except OSError:
            return None

    async def _reload_blacklist(self) -> None:
        path = self._blacklist_path
        if path is None:
            return
        mtime = self._stat_blacklist_mtime()
        if mtime is None or mtime == self._blacklist_mtime:
            return
        loop = asyncio.get_running_loop()
        try:
            fresh = await loop.run_in_executor(
                None, functools.partial(Blacklist.load, path, match_mode=self._match_mode)
            )
        except Exception:
            # A half-written file leaves the previous matcher in place; the
            # next tick retries against the completed write.
            log.exception('blacklist reload failed, keeping the previous one')
            return
        self._blacklist_mtime = mtime
        self.blacklist = fresh
        log.info('blacklist reloaded: %s', fresh.info())

    async def on_ready(self) -> None:
        log.info('logged in as %s (%s), %d guild(s)',
                 self.user, self.user.id, len(self.guilds))
        if not self.server_whitelist.ids:
            log.warning('server whitelist is empty: no servers will be monitored')
        if not self.whitelist.ids:
            log.warning('whitelist is empty: no messages will be checked or deleted')
        if WEBHOOK_ENABLED and not self.webhook_users.ids:
            log.warning('webhook user list is empty: no messages will be re-posted')
        log.info('blacklist: %s | match mode: %s',
                 self.blacklist.info(), self.blacklist.match_mode)

    # -- event handling ----------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.id == self.user.id:
            return
        if message.guild.id not in self.server_whitelist.ids:
            return

        author_id = message.author.id
        if author_id not in self.whitelist.ids:
            return

        # Only whitelisted authors need to be indexed: the index exists so
        # an edit of one of their messages can be attributed without a cache.
        self._remember(message.id, author_id)

        term = self.blacklist.check(message.content)
        if term is None:
            term = self._check_extra_text(message)
        if term is not None:
            forward = self._build_forward(message) if self._forward_allowed(author_id) else None
            self._enqueue_delete(message.channel.id, message.id, author_id, term, forward)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        data = payload.data
        guild_id = data.get('guild_id')
        if guild_id is None:
            return
        try:
            guild_id = int(guild_id)
        except (TypeError, ValueError):
            return
        if guild_id not in self.server_whitelist.ids:
            return

        author_id = self._author_index.get(payload.message_id)
        if author_id is None or author_id not in self.whitelist.ids:
            return

        content = data.get('content')
        if not isinstance(content, str) or not content:
            return

        term = self.blacklist.check(content)
        if term is not None:
            # The raw edit payload has no author name or avatar; re-post the
            # text under the webhook's own identity.
            forward = (PendingForward(content, None, None)
                       if self._forward_allowed(author_id) else None)
            self._enqueue_delete(payload.channel_id, payload.message_id, author_id, term, forward)

    # -- helpers -----------------------------------------------------------

    def _forward_allowed(self, author_id: int) -> bool:
        """Only opted-in users are re-posted; the toggle gates the whole feature."""
        return WEBHOOK_ENABLED and author_id in self.webhook_users.ids

    def _remember(self, message_id: int, author_id: int) -> None:
        index = self._author_index
        index[message_id] = author_id
        index.move_to_end(message_id)
        if len(index) > AUTHOR_INDEX_SIZE:
            index.popitem(last=False)

    def _check_extra_text(self, message: discord.Message) -> str | None:
        """Check embed text and attachment filenames when content is clean."""
        for embed in message.embeds:
            for field in (embed.title, embed.description, embed.author.name if embed.author else None,
                          embed.footer.text if embed.footer else None):
                if field:
                    term = self.blacklist.check(field)
                    if term is not None:
                        return term
            for field in embed.fields:
                term = self.blacklist.check(field.name) or self.blacklist.check(field.value)
                if term is not None:
                    return term
        for attachment in message.attachments:
            if attachment.filename:
                term = self.blacklist.check(attachment.filename)
                if term is not None:
                    return term
        return None

    def _build_forward(self, message: discord.Message) -> PendingForward:
        """Capture what the webhook re-post needs before the message is gone."""
        content = message.content
        if not content:
            parts: list[str] = []
            for embed in message.embeds:
                if embed.title:
                    parts.append(embed.title)
                if embed.description:
                    parts.append(embed.description)
                for field in embed.fields:
                    parts.append(f'{field.name}: {field.value}')
            for attachment in message.attachments:
                if attachment.filename:
                    parts.append(attachment.filename)
            content = '\n'.join(parts)

        author = message.author
        name = getattr(author, 'display_name', None) or str(author)
        name = name[:80]
        if 'discord' in name.lower() or 'clyde' in name.lower():
            # Discord rejects webhook usernames containing these words.
            name = None

        avatar = getattr(getattr(author, 'display_avatar', None), 'url', None)
        if not isinstance(avatar, str) or not avatar.startswith('https://'):
            avatar = None

        return PendingForward(content=content[:4096] or '[no text content]',
                              author_name=name, avatar_url=avatar)

    def _enqueue_delete(self, channel_id: int, message_id: int, author_id: int, term: str,
                        forward: PendingForward | None = None) -> None:
        if channel_id in self._forbidden_channels:
            return
        try:
            self._delete_queue.put_nowait((channel_id, message_id, author_id, term, forward))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 50 == 1:
                log.warning('delete queue full, %d message(s) shed so far', self._dropped)

    async def _delete_worker(self, worker_id: int) -> None:
        while True:
            channel_id, message_id, author_id, term, forward = await self._delete_queue.get()
            deleted = False
            try:
                await self.http.delete_message(channel_id, message_id)
                deleted = True
                log.info('deleted %s in channel %s from %s (term=%r)',
                         message_id, channel_id, author_id, term)
            except discord.NotFound:
                deleted = True
                log.debug('message %s already gone', message_id)
            except discord.Forbidden:
                if channel_id not in self._forbidden_channels:
                    self._forbidden_channels.add(channel_id)
                    log.error('missing Manage Messages in channel %s; skipping it', channel_id)
            except discord.HTTPException as exc:
                log.warning('delete failed for %s in %s: %s', message_id, channel_id, exc)
            finally:
                if deleted and forward is not None:
                    self._enqueue_forward(forward)
                self._delete_queue.task_done()

    def _enqueue_forward(self, item: PendingForward) -> None:
        try:
            self._forward_queue.put_nowait(item)
        except asyncio.QueueFull:
            self._forward_dropped += 1
            if self._forward_dropped % 50 == 1:
                log.warning('webhook queue full, %d re-post(s) shed so far',
                            self._forward_dropped)

    async def _forward_worker(self) -> None:
        while True:
            item = await self._forward_queue.get()
            try:
                await self._send_webhook(item)
            finally:
                self._forward_queue.task_done()

    async def _send_webhook(self, item: PendingForward) -> None:
        if self._webhook_broken:
            return
        webhook = self._webhook
        if webhook is None:
            try:
                webhook = discord.Webhook.from_url(WEBHOOK_URL, client=self)
            except (ValueError, TypeError) as exc:
                self._webhook_broken = True
                log.error('cannot use WEBHOOK_URL, re-posts disabled: %s', exc)
                return
            self._webhook = webhook

        content = item.content or '[no text content]'
        payload: dict = {'allowed_mentions': discord.AllowedMentions.none()}
        if item.author_name:
            payload['username'] = item.author_name
        if item.avatar_url:
            payload['avatar_url'] = item.avatar_url
        if len(content) <= 2000:
            payload['content'] = content
        else:
            # Webhook content caps at 2000; put the full text in an embed so
            # nothing is lost.
            payload['content'] = content[:1999] + '…'
            payload['embeds'] = [discord.Embed(description=content[:4096])]

        try:
            await webhook.send(**payload)
            log.info('re-posted a deleted message to the webhook')
        except discord.HTTPException as exc:
            status = getattr(exc, 'status', None)
            log.warning('webhook re-post failed (%s): %s', status, exc)
            if status in (401, 403, 404):
                self._webhook_broken = True
                log.error('webhook rejected further re-posts (status %s)', status)
        except Exception:
            log.exception('webhook re-post failed')

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await super().close()


def read_token() -> str:
    token = os.getenv(TOKEN_ENV, '').strip()
    if token:
        return token
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding='utf-8').strip()
        if token:
            return token
    raise SystemExit(f'set {TOKEN_ENV} or put the token in {TOKEN_FILE.name}')


def main() -> None:
    setup_logging()

    if MATCH_MODE not in ('word', 'substring'):
        raise SystemExit(f"MATCH_MODE must be 'word' or 'substring', got {MATCH_MODE!r}")
    if WEBHOOK_ENABLED:
        if not WEBHOOK_URL:
            raise SystemExit('WEBHOOK_ENABLED is true but WEBHOOK_URL is empty')
        if not WEBHOOK_URL_RE.search(WEBHOOK_URL):
            raise SystemExit('WEBHOOK_URL does not look like a Discord webhook URL')
    if not BLACKLIST_FILE.exists():
        raise SystemExit(f'blacklist not found: {BLACKLIST_FILE}')
    if not WHITELIST_FILE.exists():
        log.warning('whitelist not found at %s; nothing will be checked', WHITELIST_FILE)
    if not SERVER_WHITELIST_FILE.exists():
        log.warning('server whitelist not found at %s; no servers will be monitored',
                    SERVER_WHITELIST_FILE)

    blacklist = Blacklist.load(BLACKLIST_FILE, match_mode=MATCH_MODE)
    whitelist = IdWhitelist(WHITELIST_FILE, 'whitelist')
    server_whitelist = IdWhitelist(SERVER_WHITELIST_FILE, 'server whitelist')
    webhook_users = IdWhitelist(WEBHOOK_USERS_FILE, 'webhook user list')
    log.info('blacklist loaded from %s: %s', BLACKLIST_FILE.name, blacklist.info())
    if not server_whitelist.ids:
        log.warning('server whitelist is empty: no servers will be monitored')
    if WEBHOOK_ENABLED:
        log.info('webhook re-posting enabled')
        if not WEBHOOK_USERS_FILE.exists():
            log.warning('webhook user list not found at %s; no messages will be re-posted',
                        WEBHOOK_USERS_FILE)
        elif not webhook_users.ids:
            log.warning('webhook user list is empty: no messages will be re-posted')
        unlisted = webhook_users.ids - whitelist.ids
        if unlisted:
            log.warning('%d webhook user(s) are not in the user whitelist and will never '
                        'be forwarded', len(unlisted))

    # Memory-lean client for a 1 GB host: no message cache, no member cache,
    # no startup chunking. Guild subscriptions stay on so messages from large
    # servers still arrive.
    client = AntiReportClient(
        blacklist,
        whitelist,
        server_whitelist,
        webhook_users,
        blacklist_path=BLACKLIST_FILE,
        match_mode=MATCH_MODE,
        max_messages=None,
        member_cache_flags=discord.MemberCacheFlags.none(),
        chunk_guilds_at_startup=False,
        guild_subscriptions=True,
        sync_presence=False,
    )
    try:
        client.run(read_token(), log_handler=None, log_level=LOG_LEVEL)
    except discord.LoginFailure:
        raise SystemExit('login failed: Discord rejected the token')
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
