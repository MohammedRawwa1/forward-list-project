"""Word-form aware keyword patterns for the forward bot's searches.

Course, category and coach searches match names with a Mongo ``$regex`` built
from the user's text. Matching the raw text meant a query for "create" skipped
"Created", "Creating" and "Creation" (and a query for "creating" skipped the
plain "create" entries), so the same keyword behaved differently from the
index bots. The pattern built here covers the common English inflections of
every query word, plus a small derivational prefix set (reinstall, unlimited),
in the same order and with non-word separators between the words.

The fold is the same rule set used by the index bots (app/services/morphology.py)
so all bots recognise the same endings/startings. Tokens in other scripts
(Arabic, Persian, Cyrillic, CJK, ...) are returned unchanged.
"""

import re
from functools import lru_cache

# Derivational prefixes stripped when enough word is left behind.
_PREFIXES = (
    "under",
    "super",
    "inter",
    "multi",
    "over",
    "anti",
    "post",
    "non",
    "out",
    "mis",
    "dis",
    "pre",
    "sub",
    "un",
    "re",
)

# Inflectional / derivational suffixes and the form each folds to, longest
# first (the first match wins). Replacing instead of only trimming keeps
# "organization" -> "organiz" aligned with "organize"/"organizing" and
# "education"/"educational" -> "educat".
_REPLACEMENTS = (
    ("ational", "at"),
    ("ization", "iz"),
    ("isation", "is"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("ingly", ""),
    ("edly", ""),
    ("ments", ""),
    ("ment", ""),
    ("ness", ""),
    ("ings", ""),
    ("ing", ""),
    ("ers", ""),
    ("est", ""),
    ("ion", ""),
    ("er", ""),
    ("ed", ""),
    ("es", ""),
    ("ly", ""),
    ("s", ""),
)

_VOWELS = "aeiou"
# Letters whose doubling is usually part of the word (fall, class, buzz).
_NO_UNDOUBLE = "lsz"
# Words ending with these never lose a bare "s" (it is not a plural).
_NO_S_STRIP = ("ss", "us", "is", "os", "as")

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Words of a multi-word query must stay in order, but may be separated by any
# punctuation/whitespace ("advanced python" also matches "Advanced - Python").
_SEPARATOR = r"[\s\W_]+"


def _fold_once(word: str) -> str:
    """Strip one suffix (or turn -ies/-ied into -i)."""
    if len(word) >= 5 and word.endswith(("ies", "ied")):
        return word[:-3] + "i"

    for suffix, replacement in _REPLACEMENTS:
        if not word.endswith(suffix):
            continue
        if len(word) - len(suffix) < 3:
            continue
        if suffix == "s" and word.endswith(_NO_S_STRIP):
            continue
        base = word[: -len(suffix)] + replacement
        # running -> runn -> run, stopped -> stopp -> stop
        if len(base) >= 4 and base[-1] == base[-2] and base[-1] not in _VOWELS and base[-1] not in _NO_UNDOUBLE:
            base = base[:-1]
        return base
    return word


@lru_cache(maxsize=8192)
def fold(token: str) -> str:
    """Canonical form of a token, used on both query and indexed text."""
    word = (token or "").strip().lower()
    if len(word) < 3 or not word.isascii() or not word.isalpha():
        return word

    for _ in range(2):
        folded = _fold_once(word)
        if folded == word:
            break
        word = folded

    # "study" / "studies" -> "studi", "movie" / "movies" -> "movi"
    if len(word) >= 4 and word.endswith("y"):
        word = word[:-1] + "i"

    # "make" and "making" both end up as "mak".
    if len(word) >= 4 and word.endswith("e"):
        word = word[:-1]

    # "reinstall" / "install" -> "install".
    for prefix in _PREFIXES:
        if len(word) >= 7 and word.startswith(prefix) and len(word) - len(prefix) >= 5:
            word = word[len(prefix) :]
            break

    return word


def _variant_spellings(token: str) -> set[str]:
    """Return exact spellings that fold to the same canonical token."""
    stem = fold(token)
    roots = {stem}
    if stem and stem[-1] not in _VOWELS and stem[-1] not in _NO_UNDOUBLE:
        roots.add(stem + stem[-1])

    spellings = {token, stem}
    for root in roots:
        spellings.add(root + "e")
        if root.endswith("i"):
            spellings.update((root[:-1] + "y", root[:-1] + "ies", root[:-1] + "ied"))
        for suffix, replacement in _REPLACEMENTS:
            if replacement:
                if root.endswith(replacement):
                    spellings.add(root[: -len(replacement)] + suffix)
            else:
                spellings.add(root + suffix)
    # Keep only candidates that the canonicalizer confirms as equivalent.
    stem_spellings = {spelling for spelling in spellings if spelling and fold(spelling) == stem}
    for spelling in tuple(stem_spellings):
        for prefix in _PREFIXES:
            candidate = prefix + spelling
            if fold(candidate) == stem:
                stem_spellings.add(candidate)
    return stem_spellings


def build_search_pattern(query_text: str) -> str:
    """Regex (case-insensitive ``$regex``) matching the query in any word form.

    The pattern is deliberately unanchored: names are matched anywhere in the
    field, exactly like the previous literal ``re.escape`` patterns.
    """
    text = (query_text or "").strip()
    words = _WORD_RE.findall(text)
    if not words:
        return re.escape(text)

    parts = []
    for word in words:
        spellings = "|".join(re.escape(spelling) for spelling in sorted(_variant_spellings(word.lower())))
        parts.append(r"\b(?:" + spellings + r")\b")
    return _SEPARATOR.join(parts)
