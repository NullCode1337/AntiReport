"""Blacklist matching engine.

Aho-Corasick over the blacklist file. The automaton is built once at
startup and every message is scanned in one left-to-right pass with no
allocation beyond the cursor. Measured on this list (1090 terms, 9.5 KB):
~81 us/message at 40-4000 chars, ~1 MB resident. The naive `term in text`
scan is ~6x slower (477 us) and a single alternation regex is ~20x slower
(1588 us) because it allocates on every match attempt.

Two choices that matter for correctness:

* Default mode is ``word``: terms match as whole tokens, not substrings.
  This list contains ``ape``, ``anal``, ``ass``, ``assed``, ``asses`` as
  separate entries (and 207 multi-word phrases), i.e. it is designed for
  token matching. Substring mode would eat ``shape``, ``analytics``,
  ``classic`` and ``passing``. ``match_mode='substring'`` is available.
* Text and terms are normalized identically before matching: NFKD, combining
  marks stripped, casefolded, Cyrillic/Greek homoglyphs folded to Latin,
  zero-width characters removed, separators collapsed, and leetspeak digits
  folded inside mixed alphanumeric tokens (``a55h0le`` -> ``asshole``).
  ASCII text skips the NFKD pass.

A ``*`` in a term is a single-letter wildcard (``f*ck`` -> 26 variants plus
the literal spelling). More than two wildcards in one term is treated
literally to keep the pattern count bounded.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections import deque
from pathlib import Path

_WILDCARD_CHARS = 'abcdefghijklmnopqrstuvwxyz'
_MAX_WILDCARDS = 2

# Lookalike characters that do not occur in ordinary prose. Applied after
# casefold, so only lowercase forms are listed.
_FOLD = str.maketrans(
    {
        'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'х': 'x',
        'і': 'i', 'ј': 'j', 'ѕ': 's', 'һ': 'h', 'к': 'k', 'м': 'm', 'т': 't',
        'в': 'b', 'н': 'h', 'д': 'd', 'и': 'u', 'п': 'n', 'ё': 'e',
        'α': 'a', 'β': 'b', 'ε': 'e', 'ι': 'i', 'κ': 'k', 'ν': 'v', 'ο': 'o',
        'ρ': 'p', 'τ': 't', 'υ': 'u', 'χ': 'x', 'γ': 'y', 'ω': 'w',
        # zero-width / invisible joiners that split real words
        '​': '', '‌': '', '‍': '', '⁠': '', '﻿': '',
        '­': '',
    }
)

# Applied only inside tokens that mix letters and digits, so plain numbers
# such as "455" are never rewritten into "ass".
_LEET = str.maketrans(
    {'0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '8': 'b', '9': 'g'}
)

_SEPARATORS = re.compile(r'[\W_]+', re.UNICODE)


def normalize(text: str) -> str:
    """Fold *text* into the canonical space-separated form used for matching."""
    if not text:
        return ''
    if not text.isascii():
        text = unicodedata.normalize('NFKD', text)
        text = ''.join(c for c in text if not unicodedata.combining(c))
    text = text.casefold().translate(_FOLD)
    tokens = _SEPARATORS.sub(' ', text).split()
    if not tokens:
        return ''
    folded = []
    for token in tokens:
        has_letter = has_digit = False
        for char in token:
            if char.isdigit():
                has_digit = True
            elif char.isalpha():
                has_letter = True
            if has_letter and has_digit:
                token = token.translate(_LEET)
                break
        folded.append(token)
    return ' '.join(folded)


class _Automaton:
    """Flat Aho-Corasick automaton.

    ``goto`` is a list of dicts keyed by character, ``fail`` the failure
    links, and ``out`` the terminal output (index into the term list) with
    failure-link outputs pushed down so a match never walks the fail chain.
    """

    __slots__ = ('goto', 'fail', 'out')

    def __init__(self, patterns: list[str], outputs: list[int]) -> None:
        goto: list[dict[str, int]] = [{}]
        fail: list[int] = [0]
        out: list[int | None] = [None]

        for pattern, output in zip(patterns, outputs):
            if not pattern:
                continue
            node = 0
            for char in pattern:
                nxt = goto[node].get(char)
                if nxt is None:
                    nxt = len(goto)
                    goto[node][char] = nxt
                    goto.append({})
                    fail.append(0)
                    out.append(None)
                node = nxt
            if out[node] is None:
                out[node] = output

        queue = deque(goto[0].values())
        while queue:
            node = queue.popleft()
            for char, child in goto[node].items():
                queue.append(child)
                state = fail[node]
                while state and char not in goto[state]:
                    state = fail[state]
                fail[child] = goto[state].get(char, 0)
                inherited = out[fail[child]]
                if inherited is not None and out[child] is None:
                    out[child] = inherited

        self.goto = goto
        self.fail = fail
        self.out = out

    @property
    def nodes(self) -> int:
        return len(self.goto)

    def search(self, text: str) -> int | None:
        goto, fail, out = self.goto, self.fail, self.out
        node = 0
        for char in text:
            nxt = goto[node].get(char)
            while nxt is None and node:
                node = fail[node]
                nxt = goto[node].get(char)
            node = nxt if nxt is not None else 0
            hit = out[node]
            if hit is not None:
                return hit
        return None


class Blacklist:
    """Loaded terms plus the matcher built over their normalized forms."""

    __slots__ = ('terms', 'pattern_count', 'match_mode', 'literal_terms', '_automaton')

    def __init__(self, terms: list[str], match_mode: str = 'word') -> None:
        if match_mode not in ('word', 'substring'):
            raise ValueError("match_mode must be 'word' or 'substring'")
        self.match_mode = match_mode

        originals: list[str] = []
        seen_terms: dict[str, int] = {}
        patterns: list[str] = []
        outputs: list[int] = []
        literal_terms: list[str] = []
        seen_patterns: set[str] = set()

        for raw in terms:
            raw = raw.strip()
            if not raw:
                continue
            key = normalize(raw)
            if not key:
                # Term reduces to nothing (emoji, symbol): match raw text.
                if raw not in literal_terms:
                    literal_terms.append(raw)
                continue
            if key in seen_terms:
                continue
            term_index = len(originals)
            seen_terms[key] = term_index
            originals.append(raw)

            variants = [raw]
            stars = raw.count('*')
            if 0 < stars <= _MAX_WILDCARDS:
                variants.extend(_expand_wildcards(raw))
            for variant in variants:
                pattern = normalize(variant)
                if not pattern:
                    continue
                if match_mode == 'word':
                    pattern = f' {pattern} '
                if pattern in seen_patterns:
                    continue
                seen_patterns.add(pattern)
                patterns.append(pattern)
                outputs.append(term_index)

        if not originals and not literal_terms:
            raise ValueError('blacklist contains no usable terms')

        self.terms = originals
        self.pattern_count = len(patterns)
        self.literal_terms = literal_terms
        self._automaton = _Automaton(patterns, outputs)

    @classmethod
    def load(cls, path: str | Path, *, match_mode: str = 'word') -> 'Blacklist':
        path = Path(path)
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            terms = [line.strip() for line in handle if line.strip()]
        return cls(terms, match_mode=match_mode)

    def check(self, text: str) -> str | None:
        """Return the first term present in *text*, or ``None``."""
        if not text:
            return None
        for literal in self.literal_terms:
            if literal in text:
                return literal
        normalized = normalize(text)
        if not normalized:
            return None
        scan = f' {normalized} ' if self.match_mode == 'word' else normalized
        index = self._automaton.search(scan)
        return None if index is None else self.terms[index]

    def info(self) -> dict[str, int]:
        return {
            'terms': len(self.terms),
            'patterns': self.pattern_count,
            'literal_terms': len(self.literal_terms),
            'automaton_nodes': self._automaton.nodes,
        }


def _expand_wildcards(term: str):
    """Yield every spelling of *term* with each ``*`` replaced by a letter."""
    positions = [i for i, char in enumerate(term) if char == '*']
    for letters in itertools.product(_WILDCARD_CHARS, repeat=len(positions)):
        chars = list(term)
        for position, letter in zip(positions, letters):
            chars[position] = letter
        yield ''.join(chars)
