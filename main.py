"""Discord selfbot that deletes blacklisted messages from whitelisted users.

Runs on a user account (discord.py-self). Every guild message is checked
against the whitelist; if the author is whitelisted, the message text
(plus embed text and attachment filenames) is scanned with the Aho-Corasick
matcher from blacklist.py and the message is deleted on a hit.

Configuration comes from the real environment or from a `.env` file next to
this script (real environment wins). Variables:

    DISCORD_TOKEN       user token, required unless token.txt exists
    BLACKLIST_FILE      default: blacklist_words.txt
    WHITELIST_FILE      default: whitelist.json; users (with optional webhook
                        opt-in per entry) and servers
    REPLACEMENTS_FILE   default: replacements.json; word -> emoji map applied
                        to re-posted messages only
    MATCH_MODE          word (default) | substring
    DELETE_WORKERS      concurrent delete workers, default 2
    DELETE_QUEUE_SIZE   pending deletes before shedding, default 512
    AUTHOR_INDEX_SIZE   messages tracked for edit attribution, default 2048
    WEBHOOK_ENABLED     re-post deleted messages to a webhook, default false
    WEBHOOK_URL         webhook that receives the deleted messages
    WEBHOOK_MAX_IMAGES  image attachments re-posted per message, default 4
    WEBHOOK_MAX_IMAGE_MB per-image size cap for re-posting, default 8
    LOG_FILE            optional rotating log path (stdout always logs)
    LOG_LEVEL           default INFO

Relative paths in BLACKLIST_FILE/WHITELIST_FILE/REPLACEMENTS_FILE resolve
against this script's directory, not the process working directory.

Replacements format (replacements.json): keys are the replacement text (any
Discord emoji form, e.g. ":skull:" or "<:name:123456789012345678>"), values
are the words replaced by it, either a string or an array:

The substitution happens only in the webhook re-post, never on the server,
and uses the same MATCH_MODE as the blacklist.

Whitelist format (whitelist.json): a JSON object with two arrays —

    {
      "users": [
        123456789012345678,
        {"id": 234567890123456789, "webhook": true}
      ],
      "servers": [345678901234567890]
    }

Only listed guilds are monitored, and only listed users are checked. A user
entry is either a bare numeric ID or an object with "id" and an optional
"webhook" flag (default false); "webhook": true re-posts that user's deleted
messages to WEBHOOK_URL. All config files are read once at startup; restart
the process after editing them.
"""

from __future__ import annotations

import asyncio
import ctypes
import gc
import io
import json
import logging
import os
import random
import re
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NamedTuple

import discord

from blacklist import Blacklist
from replacements import Replacements

BASE_DIR = Path(__file__).resolve().parent


def _trim_allocator_arenas() -> None:
    """Cap glibc malloc arenas before any threads exist.

    Each arena reserves address space and inflates RSS on multi-threaded
    processes (the aiohttp resolver spawns threads). Two arenas is plenty for
    this process and cuts resident memory on the 1 GB target host.
    """
    if not sys.platform.startswith('linux'):
        return
    try:
        ctypes.CDLL('libc.so.6').mallopt(-8, 2)  # M_ARENA_MAX
    except OSError:
        pass


_trim_allocator_arenas()


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
WHITELIST_FILE = _env_path('WHITELIST_FILE', BASE_DIR / 'whitelist.json')
REPLACEMENTS_FILE = _env_path('REPLACEMENTS_FILE', BASE_DIR / 'replacements.json')
MATCH_MODE = os.getenv('MATCH_MODE', 'word').strip().lower()
DELETE_WORKERS = max(1, int(os.getenv('DELETE_WORKERS', '2')))
DELETE_QUEUE_SIZE = max(16, int(os.getenv('DELETE_QUEUE_SIZE', '512')))
WEBHOOK_ENABLED = _env_bool('WEBHOOK_ENABLED', False)
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '').strip()
# Image attachments are downloaded into memory and re-uploaded as
# multipart/form-data.
WEBHOOK_MAX_IMAGES = max(0, int(os.getenv('WEBHOOK_MAX_IMAGES', '4')))
WEBHOOK_MAX_IMAGE_BYTES = max(
    0, int(float(os.getenv('WEBHOOK_MAX_IMAGE_MB', '8')) * 1024 * 1024))
IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.avif'})
LOG_FILE = os.getenv('LOG_FILE')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

# Same shape the library accepts in Webhook.from_url; checked at startup so a
# typo fails before the account connects.
WEBHOOK_URL_RE = re.compile(r'discord(?:app)?\.com/api/webhooks/\d{17,20}/[A-Za-z0-9.\-_]{60,}')

# Re-posts waiting behind webhook rate limits. Bounded; a full queue sheds.
FORWARD_QUEUE_SIZE = 256

# Command token for webhook forwarding. As a reply whose whole text is this
# token, it deletes the replied-to message and the command and re-posts the
# replied-to message. In a message that is not a reply, the token may appear
# anywhere: the message is deleted, the token is trimmed out, and the rest is
# re-posted. Only the account itself can issue it, and only while the webhook
# is enabled; otherwise the command does nothing.
FW_COMMAND = '///fw'

# Bounded author index for edit detection: message id -> author id. Raw
# MESSAGE_UPDATE payloads do not carry the author, so an edit can only be
# attributed if the original message was seen while it was still recent.
AUTHOR_INDEX_SIZE = max(256, int(os.getenv('AUTHOR_INDEX_SIZE', '2048')))

# A hit is acted on after a short random pause so deletion and re-posting do
# not happen the instant the message is read.
ACTION_DELAY_MIN = 0.5
ACTION_DELAY_MAX = 1.0

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


def _coerce_id(entry: object) -> int | None:
    """Return an ID from an int or a digit-only string; None for anything else."""
    if isinstance(entry, bool):
        return None
    if isinstance(entry, int):
        return entry
    if isinstance(entry, str):
        text = entry.strip()
        if text.isdigit():
            return int(text)
    return None


def _id_set(value: object, label: str, path: Path) -> frozenset[int]:
    """Parse a JSON array of numeric IDs; non-numbers are skipped with a warning."""
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        log.warning('whitelist %s: %r must be an array, ignoring it', path.name, label)
        return frozenset()
    ids: set[int] = set()
    for entry in value:
        if isinstance(entry, bool):
            log.warning('whitelist %s: ignoring boolean %s entry', path.name, label)
            continue
        entry_id = _coerce_id(entry)
        if entry_id is None:
            log.warning('whitelist %s: ignoring non-numeric %s entry %r',
                        path.name, label, entry)
        else:
            ids.add(entry_id)
    return frozenset(ids)


def _user_sets(value: object, path: Path) -> tuple[frozenset[int], frozenset[int]]:
    """Parse the users array into (users, webhook_users).

    An entry is either a bare ID or an object ``{"id": <id>, "webhook": <bool>}``
    where "webhook" defaults to false. The opt-in set is derived from the user
    entries, so each ID appears once.
    """
    if value is None:
        return frozenset(), frozenset()
    if not isinstance(value, list):
        log.warning('whitelist %s: %r must be an array, ignoring it', path.name, 'users')
        return frozenset(), frozenset()

    users: set[int] = set()
    webhook_users: set[int] = set()
    for entry in value:
        if isinstance(entry, dict):
            if 'id' not in entry:
                log.warning('whitelist %s: ignoring user object without "id": %r',
                            path.name, entry)
                continue
            user_id = _coerce_id(entry['id'])
            if user_id is None:
                log.warning('whitelist %s: ignoring user with non-numeric id %r',
                            path.name, entry['id'])
                continue
            flag = entry.get('webhook', False)
            if not isinstance(flag, bool):
                log.warning('whitelist %s: user %s: "webhook" must be true or false, '
                            'ignoring the entry', path.name, user_id)
                continue
            users.add(user_id)
            if flag:
                webhook_users.add(user_id)
            continue

        if isinstance(entry, bool):
            log.warning('whitelist %s: ignoring boolean users entry', path.name)
            continue
        user_id = _coerce_id(entry)
        if user_id is None:
            log.warning('whitelist %s: ignoring non-numeric users entry %r',
                        path.name, entry)
            continue
        users.add(user_id)

    return frozenset(users), frozenset(webhook_users)


class Whitelists:
    """Users, servers, and webhook opt-ins read once from one JSON file."""

    __slots__ = ('users', 'servers', 'webhook_users')

    def __init__(self, path: Path) -> None:
        self.users: frozenset[int] = frozenset()
        self.servers: frozenset[int] = frozenset()
        self.webhook_users: frozenset[int] = frozenset()

        try:
            with path.open('r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            # ValueError covers a malformed or half-written JSON file.
            log.error('cannot read whitelist %s: %s', path, exc)
            return

        if not isinstance(data, dict):
            log.error('whitelist %s: top level must be a JSON object', path)
            return

        if 'webhook_users' in data:
            log.warning('whitelist %s: "webhook_users" is ignored; set "webhook": true on '
                        'the user entries instead', path)

        self.users, self.webhook_users = _user_sets(data.get('users'), path)
        self.servers = _id_set(data.get('servers'), 'servers', path)
        log.info('whitelist loaded: %d user(s), %d server(s), %d webhook user(s)',
                 len(self.users), len(self.servers), len(self.webhook_users))


class PendingForward(NamedTuple):
    """Message data captured before deletion for the webhook re-post."""

    content: str
    author_name: str | None
    avatar_url: str | None
    attachments: tuple[object, ...] = ()
    reply_user_id: int | None = None


def _is_image_attachment(attachment: object) -> bool:
    """True when the attachment advertises an image type or image extension."""
    content_type = getattr(attachment, 'content_type', None)
    if isinstance(content_type, str) and content_type.lower().startswith('image/'):
        return True
    name = getattr(attachment, 'filename', None)
    if not isinstance(name, str):
        return False
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def _safe_upload_name(name: object, fallback: str = 'image') -> str:
    """Strip directories and header-hostile characters from an upload name."""
    text = name if isinstance(name, str) else ''
    text = text.replace('\\', '/').rsplit('/', 1)[-1]
    text = ''.join(ch for ch in text if ch.isprintable() and ch != '"')
    text = text.strip() or fallback
    return text[:100]


def _raw_reply_target(data: dict) -> int | None:
    """Return the replied-to author's ID from a raw MESSAGE_UPDATE payload."""
    reference = data.get('message_reference')
    if not isinstance(reference, dict):
        return None
    if reference.get('type', 0) != 0:
        return None
    resolved = data.get('referenced_message')
    author = resolved.get('author') if isinstance(resolved, dict) else None
    user_id = author.get('id') if isinstance(author, dict) else None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def _reply_target(message: discord.Message) -> int | None:
    """Return the replied-to author's ID, if this message is a reply.

    A forwarded message also carries a message_reference, so only type
    "reply" counts. When Discord did not resolve the referenced message the
    author is simply not recoverable from here and the reply prefix is
    skipped.
    """
    reference = getattr(message, 'reference', None)
    if reference is None:
        return None
    if getattr(reference, 'type', discord.MessageReferenceType.reply) != discord.MessageReferenceType.reply:
        return None
    resolved = getattr(reference, 'resolved', None)
    user_id = getattr(getattr(resolved, 'author', None), 'id', None)
    return user_id if isinstance(user_id, int) else None


def _strip_fw_command(content: str) -> str:
    """Remove the ``///fw`` token from a message body and tidy the leftovers.

    The token is replaced with a space so ``word///fwword`` does not merge into
    one word, then runs of spaces and tabs collapse to a single space. Newlines
    are preserved so multi-line messages keep their shape.
    """
    cleaned = re.sub(r'[ \t]+', ' ', content.replace(FW_COMMAND, ' '))
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    return cleaned.strip()


def _close_upload(file: discord.File) -> None:
    """Release the in-memory buffer behind an upload file.

    discord.File stubs the wrapped buffer's close() and File.close() only
    restores that stub for a caller-owned BytesIO, so close the buffer itself
    to free the bytes.
    """
    try:
        file.close()
    except Exception:
        pass
    fp = getattr(file, 'fp', None)
    close = getattr(fp, 'close', None)
    if close is not None:
        try:
            close()
        except Exception:
            pass


class AntiReportClient(discord.Client):
    def __init__(self, blacklist: Blacklist, whitelists: Whitelists,
                 replacements: Replacements | None = None, **options) -> None:
        super().__init__(**options)
        # This client only consumes MESSAGE_CREATE and MESSAGE_UPDATE. Dropping
        # the parsers for noisy unused events avoids per-event allocations;
        # PRESENCE_UPDATE in particular floods large guilds.
        parsers = self._connection.parsers
        for event in (
            'PRESENCE_UPDATE', 'TYPING_START', 'MESSAGE_ACK',
            'MESSAGE_REACTION_ADD', 'MESSAGE_REACTION_REMOVE',
            'MESSAGE_REACTION_REMOVE_ALL', 'MESSAGE_REACTION_REMOVE_EMOJI',
            'MESSAGE_POLL_VOTE_ADD', 'MESSAGE_POLL_VOTE_REMOVE',
            'GUILD_MEMBER_ADD', 'GUILD_MEMBER_UPDATE', 'GUILD_MEMBER_REMOVE',
            'GUILD_MEMBERS_CHUNK', 'VOICE_STATE_UPDATE',
        ):
            parsers.pop(event, None)
        self.blacklist = blacklist
        self.whitelists = whitelists
        self.replacements = replacements
        self._delete_queue: asyncio.Queue = asyncio.Queue(maxsize=DELETE_QUEUE_SIZE)
        self._workers: list[asyncio.Task] = []
        self._author_index: dict[int, int] = {}
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

        loop = asyncio.get_running_loop()
        # aiohttp resolves DNS through the loop's default executor. One worker
        # thread is enough here and avoids a pool sized to the host's CPU count.
        try:
            loop.set_default_executor(
                ThreadPoolExecutor(max_workers=1, thread_name_prefix='antireport'))
        except RuntimeError:
            pass

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.close()))
            except (NotImplementedError, RuntimeError):
                pass

    async def on_ready(self) -> None:
        log.info('logged in as %s (%s), %d guild(s)',
                 self.user, self.user.id, len(self.guilds))
        if not self.whitelists.servers:
            log.warning('server whitelist is empty: no servers will be monitored')
        if not self.whitelists.users:
            log.warning('whitelist is empty: no messages will be checked or deleted')
        if WEBHOOK_ENABLED and not self.whitelists.webhook_users:
            log.warning('no users have "webhook": true, so nothing will be re-posted')
        log.info('manual forward: %s', 'available: reply with ' + FW_COMMAND
                 if WEBHOOK_ENABLED else 'disabled: webhook is off')
        log.info('blacklist: %s | match mode: %s',
                 self.blacklist.info(), self.blacklist.match_mode)
        state = self._connection
        getattr(state, '_guild_presences', {}).clear()
        getattr(state, '_presences', {}).clear()

    # -- event handling ----------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        # Trace every carrier of the token before the author filter. If a
        # command appears to do nothing, this line distinguishes an event that
        # never reached the client from a mismatch between the sending account
        # and the logged-in account, which no handler-side log can show.
        content = message.content or ''
        if FW_COMMAND in content:
            log.info('fw: saw %s from %s (self=%s, guild=%s): %r',
                     message.id, message.author.id,
                     message.author.id == self.user.id,
                     getattr(message.guild, 'id', None), content[:80])

        # The operator's own posts drive ``///fw`` and must be seen in DMs too,
        # where ``message.guild`` is None. Authorship is checked first for that
        # reason; everything below only applies to guild messages from others.
        if message.author.id == self.user.id:
            self._handle_fw_command(message)
            return
        if message.guild is None:
            return
        if message.guild.id not in self.whitelists.servers:
            return

        author_id = message.author.id
        if author_id not in self.whitelists.users:
            return

        # A whitelisted sender may use the reply form of the manual forward:
        # replying with exactly the token deletes the replied-to message and
        # the command and re-posts the target. The non-reply form stays
        # restricted to the account itself, so the token appearing in ordinary
        # chat can never delete the message carrying it.
        if content.strip() == FW_COMMAND:
            reference = getattr(message, 'reference', None)
            ref_type = getattr(reference, 'type', discord.MessageReferenceType.reply)
            if reference is not None and ref_type == discord.MessageReferenceType.reply:
                self._handle_fw_command(message)
                return
            log.info('fw: ignoring %s from %s: whitelisted senders may only use '
                     'the reply form', message.id, author_id)

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
        if guild_id not in self.whitelists.servers:
            return

        author_id = self._author_index.get(payload.message_id)
        if author_id is None or author_id not in self.whitelists.users:
            return

        content = data.get('content')
        if not isinstance(content, str) or not content:
            return

        term = self.blacklist.check(content)
        if term is not None:
            forward = None
            if self._forward_allowed(author_id):
                # The raw edit payload has no author name or avatar; re-post the
                # text under the webhook's own identity. Attachments are rebuilt
                # from the payload so images survive an edit-triggered delete.
                if self.replacements is not None:
                    content = self.replacements.apply(content)
                forward = PendingForward(content, None, None,
                                         self._raw_images(data.get('attachments')),
                                         _raw_reply_target(data))
            self._enqueue_delete(payload.channel_id, payload.message_id, author_id, term, forward)

    def _handle_fw_command(self, message: discord.Message) -> None:
        """Handle ``///fw`` from the account itself or a whitelisted sender.

        As a reply whose whole text is ``///fw``: delete the replied-to message
        and the command, then re-post the replied-to message. In a message that
        is not a reply: the token may appear anywhere, the message is deleted,
        the token is trimmed out, and the remaining text is re-posted. The
        non-reply form is only reachable for the account itself; the caller
        gates whitelisted senders to the reply form. The whole command is inert
        unless the webhook is enabled: without a webhook there is nothing to
        forward to, so nothing is deleted either.
        """
        content = message.content or ''
        if FW_COMMAND not in content:
            # Every self-authored message arrives here. Only one carrying the
            # token is a command attempt, so this is the only silent exit.
            return
        if not WEBHOOK_ENABLED:
            log.info('fw: ignoring %s: webhook is disabled', message.id)
            return

        reference = getattr(message, 'reference', None)
        ref_type = getattr(reference, 'type', discord.MessageReferenceType.reply)
        if reference is not None and ref_type == discord.MessageReferenceType.reply:
            if content.strip() != FW_COMMAND:
                log.info('fw: ignoring reply %s: text is not exactly %s',
                         message.id, FW_COMMAND)
                return
            self._forward_reply_target(message, reference)
            return

        # Not a reply: re-post this message's own text with the token removed.
        # A bare "///fw" leaves no text, so only a message carrying attachments
        # or embeds still produces a re-post; otherwise it is just deleted.
        trimmed = _strip_fw_command(content)
        if trimmed:
            forward = self._build_forward(message, trimmed)
        elif message.attachments or message.embeds:
            forward = self._build_forward(message, '')
        else:
            forward = None
        if self._enqueue_delete(message.channel.id, message.id, message.author.id,
                                FW_COMMAND, forward):
            if forward is None:
                log.info('fw: deleting %s (no text left to re-post)', message.id)
            else:
                log.info('fw: deleting %s and re-posting its trimmed text', message.id)

    def _forward_reply_target(self, message: discord.Message, reference) -> None:
        """Delete a ``///fw`` reply target and the command, re-posting the target."""
        target_id = getattr(reference, 'message_id', None)
        if not isinstance(target_id, int):
            log.info('fw: ignoring %s: reply reference has no message id', message.id)
            return
        channel_id = getattr(reference, 'channel_id', None)
        if not isinstance(channel_id, int):
            channel_id = message.channel.id

        target = getattr(reference, 'resolved', None)
        if isinstance(target, discord.Message):
            author_id = target.author.id
            forward = self._build_forward(target)
            target_id = target.id
        else:
            # The replied-to message exists by ID but its content was not in the
            # payload, so there is nothing to re-post. It is still deleted when
            # the channel allows it. A fetch is deliberately not attempted: a
            # resolved message is present on replies in practice, and an extra
            # API call per command is not worth covering the rare gap.
            author_id = 0
            forward = None
            log.warning('fw: replied-to message %s is unavailable; deleting it '
                        'without a re-post', target_id)

        # Only the account's own messages are deletable in a DM or group DM; in
        # a guild, Manage Messages covers the target. Attempting a delete that
        # cannot succeed would 403 and mark the whole channel forbidden, which
        # would silently kill later work there.
        own_target = isinstance(target, discord.Message) and (
            self.user is not None and target.author.id == self.user.id)
        deletable = message.guild is not None or own_target

        if deletable:
            # The re-post rides on the target delete, so a forbidden or dropped
            # delete does not fabricate a webhook entry.
            queued = self._enqueue_delete(channel_id, target_id, author_id,
                                          FW_COMMAND, forward)
            self._enqueue_delete(message.channel.id, message.id, message.author.id, FW_COMMAND)
            if queued:
                log.info('fw: deleting %s and %s', target_id, message.id)
            return

        if forward is not None:
            # Target cannot be removed here (someone else's DM message); re-post
            # it and delete only the command.
            if self._enqueue_delete(message.channel.id, message.id, message.author.id,
                                    FW_COMMAND, forward):
                log.info('fw: deleting %s and re-posting target %s '
                         '(target not deletable in this channel)', message.id, target_id)
        else:
            # The target cannot be deleted or re-posted here; still remove the
            # command so it does not linger.
            if self._enqueue_delete(message.channel.id, message.id, message.author.id,
                                    FW_COMMAND):
                log.info('fw: deleting %s (target %s not deletable and unavailable)',
                         message.id, target_id)

    # -- helpers -----------------------------------------------------------

    def _forward_allowed(self, author_id: int) -> bool:
        """Only opted-in users are re-posted; the toggle gates the whole feature."""
        return WEBHOOK_ENABLED and author_id in self.whitelists.webhook_users

    def _remember(self, message_id: int, author_id: int) -> None:
        index = self._author_index
        # A plain dict is insertion-ordered; message IDs increase, so the first
        # key is always the oldest and evicting it is O(1).
        index.pop(message_id, None)
        index[message_id] = author_id
        if len(index) > AUTHOR_INDEX_SIZE:
            index.pop(next(iter(index)))

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

    def _build_forward(self, message: discord.Message,
                       override: str | None = None) -> PendingForward:
        """Capture what the webhook re-post needs before the message is gone.

        ``override`` replaces the message text, used by ``///fw`` to re-post a
        body with the command token trimmed out. Everything else (author,
        images, reply mention) still comes from ``message``.
        """
        content = message.content if override is None else override
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

        if self.replacements is not None:
            # Only the re-post is rewritten; the stored message is untouched.
            content = self.replacements.apply(content)

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
                              author_name=name, avatar_url=avatar,
                              attachments=self._forward_images(message),
                              reply_user_id=_reply_target(message))

    def _forward_images(self, message: discord.Message) -> tuple[object, ...]:
        """Pick image attachments to re-post, bounded by the configured caps."""
        return self._select_images(message.attachments)

    def _raw_images(self, raw: object) -> tuple[object, ...]:
        """Rebuild attachments from a raw MESSAGE_UPDATE payload.

        Attachment.read() only needs the connection's HTTP client, so the
        library class works from the raw dicts without a cached Message.
        """
        if not isinstance(raw, list) or not raw:
            return ()
        rebuilt: list[object] = []
        for data in raw:
            if not isinstance(data, dict):
                continue
            try:
                rebuilt.append(discord.Attachment(data=data, state=self._connection))
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                log.debug('skipping malformed attachment in edit payload: %s', exc)
        return self._select_images(rebuilt)

    def _select_images(self, attachments) -> tuple[object, ...]:
        if WEBHOOK_MAX_IMAGES <= 0 or WEBHOOK_MAX_IMAGE_BYTES <= 0:
            return ()
        selected: list[object] = []
        for attachment in attachments:
            if len(selected) >= WEBHOOK_MAX_IMAGES:
                break
            if not _is_image_attachment(attachment):
                continue
            size = getattr(attachment, 'size', 0) or 0
            if isinstance(size, int) and size > WEBHOOK_MAX_IMAGE_BYTES:
                log.warning('not re-posting image %r: %d bytes is over the %d-byte limit',
                            getattr(attachment, 'filename', '?'), size,
                            WEBHOOK_MAX_IMAGE_BYTES)
                continue
            selected.append(attachment)
        return tuple(selected)

    def _enqueue_delete(self, channel_id: int, message_id: int, author_id: int, term: str,
                        forward: PendingForward | None = None) -> bool:
        """Queue a delete and report whether it was queued rather than dropped.

        Callers that attach a webhook re-post to the delete need the answer:
        a dropped delete would otherwise log success and silently lose the
        forward with it.
        """
        if channel_id in self._forbidden_channels:
            # A previous delete in this channel returned 403. Dropping the work
            # is correct, but staying silent makes "nothing happened" look like
            # a bug in the caller, so say why. INFO so journalctl shows it.
            log.info('skipping delete of %s in %s: channel is marked forbidden',
                     message_id, channel_id)
            return False
        try:
            self._delete_queue.put_nowait((channel_id, message_id, author_id, term, forward))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 50 == 1:
                log.warning('delete queue full, %d message(s) shed so far', self._dropped)
            return False
        return True

    async def _delete_worker(self, worker_id: int) -> None:
        while True:
            channel_id, message_id, author_id, term, forward = await self._delete_queue.get()
            deleted = False
            try:
                await asyncio.sleep(random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX))
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
                await asyncio.sleep(random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX))
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
        if item.reply_user_id is not None:
            content = f'<@{item.reply_user_id}> {content}'
        payload: dict = {'allowed_mentions': discord.AllowedMentions.none()}
        if item.reply_user_id is not None:
            # Nothing else may ping, but the mention must render as a user tag.
            payload['allowed_mentions'] = discord.AllowedMentions(
                everyone=False, roles=False, users=[discord.Object(id=item.reply_user_id)],
                replied_user=False)
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

        files: list[discord.File] = []
        for attachment in item.attachments:
            file = await self._download_image(attachment)
            if file is not None:
                files.append(file)
        if files:
            payload['files'] = files

        try:
            await webhook.send(**payload)
            log.info('re-posted a deleted message to the webhook (%d image(s))', len(files))
        except discord.HTTPException as exc:
            status = getattr(exc, 'status', None)
            log.warning('webhook re-post failed (%s): %s', status, exc)
            if status in (401, 403, 404):
                self._webhook_broken = True
                log.error('webhook rejected further re-posts (status %s)', status)
        except Exception:
            log.exception('webhook re-post failed')
        finally:
            for file in files:
                _close_upload(file)

    async def _download_image(self, attachment: object) -> discord.File | None:
        """Fetch one image into memory for a multipart upload; None on skip/failure."""
        read = getattr(attachment, 'read', None)
        if read is None:
            return None
        name = getattr(attachment, 'filename', 'image')
        size = getattr(attachment, 'size', 0) or 0
        if isinstance(size, int) and size > WEBHOOK_MAX_IMAGE_BYTES:
            log.warning('not re-posting image %r: %d bytes is over the %d-byte limit',
                        name, size, WEBHOOK_MAX_IMAGE_BYTES)
            return None
        try:
            # proxy_url survives message deletion more often than the CDN URL.
            data = await read(use_cached=True)
        except (discord.HTTPException, OSError) as exc:
            log.warning('cannot download image %r for the webhook: %s', name, exc)
            return None
        if not isinstance(data, (bytes, bytearray)) or not data:
            log.warning('image %r returned no data; sending the message without it', name)
            return None
        if len(data) > WEBHOOK_MAX_IMAGE_BYTES:
            log.warning('not re-posting image %r: %d bytes is over the %d-byte limit',
                        name, len(data), WEBHOOK_MAX_IMAGE_BYTES)
            return None
        # BytesIO keeps the upload in memory; nothing is written to disk.
        return discord.File(io.BytesIO(bytes(data)), filename=_safe_upload_name(name))

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
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
        log.warning('whitelist file not found at %s; no servers or users are configured',
                    WHITELIST_FILE)

    blacklist = Blacklist.load(BLACKLIST_FILE, match_mode=MATCH_MODE)
    whitelists = Whitelists(WHITELIST_FILE)
    replacements = Replacements(REPLACEMENTS_FILE, match_mode=MATCH_MODE)
    log.info('blacklist loaded from %s: %s', BLACKLIST_FILE.name, blacklist.info())
    if replacements.count:
        log.info('replacements loaded from %s: %d word(s)',
                 REPLACEMENTS_FILE.name, replacements.count)
    elif REPLACEMENTS_FILE.exists():
        log.warning('replacements file %s has no usable entries', REPLACEMENTS_FILE)
    if not whitelists.servers:
        log.warning('server whitelist is empty: no servers will be monitored')
    if not whitelists.users:
        log.warning('user whitelist is empty: no messages will be checked or deleted')
    if WEBHOOK_ENABLED:
        log.info('webhook re-posting enabled')
        if not whitelists.webhook_users:
            log.warning('no users have "webhook": true, so nothing will be re-posted')

    gc.freeze()

    # Memory-lean client for a 1 GB host: no message cache, no member cache,
    # no startup chunking. Guild subscriptions stay on so messages from large
    # servers still arrive.
    client = AntiReportClient(
        blacklist,
        whitelists,
        replacements,
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
