"""Emoji substitution applied to re-posted messages.

A replacements file is a JSON object whose keys are the replacement text (any
Discord emoji form) and whose values are the words that map to it:

    {
      ":emoji_name:": ["word", "another word"],
      "<:custom:123456789012345678>": ["third word"],
      ":skull:": "solo word"
    }

A value may be a single string or an array of strings; several words sharing
one emoji is the common case. Entries are casefolded and split into tokens, so
a multi-word entry matches however the message separates the words
(``dumb ass``, ``dumb-ass``, ``dumb_ass``). In ``word`` mode an entry must
span whole tokens, so ``ass`` does not match inside ``classic``; in
``substring`` mode it matches anywhere. The replacement is inserted verbatim,
so any Discord emoji form (unicode, ``:shortcode:``, ``<:name:id>``) works.

The whole list compiles to one alternation regex: a message with no matching
token costs a single failed ``search()``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger('antireport')

# A word character; the boundaries below keep "ass" out of "classic".
_BOUNDARY = r'[^\W_]'
_TOKEN = re.compile(r'[^\W_]+')
# Any run of separators between the tokens of a multi-word entry. Underscore
# counts as a separator here, matching blacklist normalization.
_SEPARATOR = r'[\W_]+'


def _key(text: str) -> str:
    """Canonical form of an entry: lowercase tokens joined by single spaces."""
    return ' '.join(_TOKEN.findall(text.casefold()))


def _pattern_for(key: str, match_mode: str) -> str:
    tokens = [re.escape(token) for token in key.split()]
    body = _SEPARATOR.join(tokens)
    if match_mode == 'substring':
        return body
    return rf'(?<!{_BOUNDARY})(?:{body})(?!{_BOUNDARY})'


class Replacements:
    """Compiled word -> emoji map loaded from a JSON file."""

    __slots__ = ('count', 'match_mode', '_pattern', '_lookup')

    def __init__(self, path: Path, match_mode: str = 'word') -> None:
        self.count = 0
        self.match_mode = match_mode
        self._pattern: re.Pattern[str] | None = None
        self._lookup: dict[str, str] = {}
        self._load(path)

    def _load(self, path: Path) -> None:
        try:
            with path.open('r', encoding='utf-8') as handle:
                data = json.load(handle)
        except FileNotFoundError:
            # Optional feature: an absent file simply disables replacement.
            return
        except (OSError, ValueError) as exc:
            log.error('cannot read replacements %s: %s', path, exc)
            return

        if not isinstance(data, dict):
            log.error('replacements %s: top level must be a JSON object', path)
            return

        lookup: dict[str, str] = {}
        for emoji, words in data.items():
            if not isinstance(emoji, str) or not emoji.strip():
                log.warning('replacements %s: ignoring non-string key %r', path, emoji)
                continue
            emoji = emoji.strip()
            if isinstance(words, str):
                words = [words]
            if not isinstance(words, list):
                log.warning('replacements %s: %r must be a string or array', path, emoji)
                continue
            for word in words:
                if not isinstance(word, str) or not word.strip():
                    log.warning('replacements %s: ignoring non-string word %r', path, word)
                    continue
                key = _key(word)
                if not key:
                    log.warning('replacements %s: %r has no letter or digit tokens',
                                path, word.strip())
                    continue
                if key in lookup:
                    log.warning('replacements %s: %r already maps to %r; keeping the '
                                'first mapping', path, word.strip(), lookup[key])
                    continue
                lookup[key] = emoji

        if not lookup:
            return

        self._lookup = lookup
        self.count = len(lookup)
        # Longest first so an overlapping entry cannot swallow the specific one.
        alternation = '|'.join(
            _pattern_for(key, self.match_mode)
            for key in sorted(lookup, key=len, reverse=True))
        self._pattern = re.compile(alternation, re.IGNORECASE)

    def apply(self, text: str) -> str:
        """Replace every known entry in *text* with its emoji, or return it as is."""
        pattern = self._pattern
        if pattern is None or not text:
            return text
        lookup = self._lookup

        def _sub(match: re.Match[str]) -> str:
            return lookup.get(_key(match.group(0)), match.group(0))

        return pattern.sub(_sub, text)

    def info(self) -> dict[str, int]:
        return {'words': self.count}
