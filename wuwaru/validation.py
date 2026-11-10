"""Conservative structural validation, independent of providers and PAK readers."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

# Preserve exact order as well as spelling. Unknown markup is not auto-repaired.
TOKEN = re.compile(
    r'<[^<>\r\n]*>|\{\{[^{}]*\}\}|\$\{[^{}]*\}|\{[^{}]*\}|'
    r'\$\([^()]*\)|\$[A-Za-z_][\w.]*|%[A-Za-z_]\w*%|'
    r'(?<!\d)%(?:\d+\$)?[-+#0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]*[diuoxXfFeEgGcsapn%]|'
    r'\\[nrt]|\r\n|\n|\r|\t')
CYRILLIC = re.compile('[А-Яа-яЁё]')
# Ideographs and Hangul are text; kana only from two letters in a row. A lone kana is part of
# a kaomoji such as "\u30fe(\uff9f\u25bd\uff9f)\u30ce" inside an English phone message.
FOREIGN_TEXT = re.compile('[\u3400-\u9fff\uac00-\ud7af]|[\u3040-\u30ff]{2}')


# "I..." / "I—": a speaker trailing off. A bare "I" or "I-1" is a grade or a numeral and stays.
TRAILING_I = re.compile(r'(?<![A-Za-z])I(?=\s*(?:\.\.|…|—|–))')


def foreign_text(shown: str) -> bool:
    return bool(FOREIGN_TEXT.search(shown))


# "называл(а)": both genders at once. Only after a past-tense ending, so "значение (а)" stays prose.
GENDER_BRACKETS = re.compile(r'[а-яё](?:л|лся)\((?:а|ла|ась)\)', re.I)
# These tokens carry words the reader sees: the keys are structure, the values are text to translate.
TRANSLATABLE = (
    (re.compile(r'^\{Male=[^;{}]+;Female=[^;{}]+\}$'), '{Male=;Female=}'),
    (re.compile(r'^\{Cus:Ipt,Touch=.+? PC=.+? Gamepad=.+\}$'), '{Cus:Ipt,Touch= PC= Gamepad=}'),
    (re.compile(r'^\{Cus:Sap,S=.+? P=.+? (SapTag=[^ {}]+)\}$'), r'{Cus:Sap,S= P= \1}'),
    (re.compile(r'^<ano=[^<>]+>$'), '<ano=>'),
)


def needs_translation(identifier: str, source: str) -> bool:
    """Empty cells, masked stars, numbers, "x{0}", CJK leftovers and original song lyrics cost money and give nothing."""
    if identifier.startswith('LyricsText_') and identifier.endswith('_Original'):
        return False
    shown = TOKEN.sub('', source)
    letters = len(re.findall('[A-Za-z]', shown))
    return (letters >= 2 or (letters == 1 and bool(TRAILING_I.search(shown)))) and not foreign_text(shown)


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


def shape(token: str) -> str:
    for rule, frozen in TRANSLATABLE:
        if rule.match(token):
            return rule.sub(frozen, token)
    return token


@lru_cache(maxsize=None)
def _bad_char(c: str) -> bool:
    category = unicodedata.category(c)
    return (c == '�' or category in ('Cs', 'Cf') or (category == 'Cc' and c not in '\r\n\t')
            or (ord(c) & 0xFFFF) in (0xFFFE, 0xFFFF))


@lru_cache(maxsize=None)
def _mark(c: str) -> bool:
    return unicodedata.category(c) == 'Mn'


def validate(source: str, target: str | None) -> list[str]:
    errors = []
    if not isinstance(target, str):
        return ['MISSING']
    if len(target.encode('utf-8', errors='surrogatepass')) > 4 * 1024 * 1024:
        errors.append('TOO_LARGE')
    # Each distinct character once, answers cached: per-character category calls took seconds per package.
    if any(map(_bad_char, set(target))):
        errors.append('UNICODE')
    # A stress mark over a Russian letter passes every other check; how the game font draws it is UNKNOWN.
    if (any(map(_mark, set(unicodedata.normalize('NFC', target))))
            and not any(map(_mark, set(unicodedata.normalize('NFC', source))))):
        errors.append('COMBINING_MARK')
    if GENDER_BRACKETS.search(target):
        errors.append('GENDER_BRACKETS')
    source_tokens, target_tokens = tokens(source), tokens(target)
    if list(map(shape, source_tokens)) != list(map(shape, target_tokens)):
        errors.append('TOKENS')
    elif any(shape(a) != a and a == b for a, b in zip(source_tokens, target_tokens)):
        errors.append('TOKEN_VALUE')
    prose = TOKEN.sub('', source)
    translated = TOKEN.sub('', target)
    if prose.strip() and not translated.strip():
        errors.append('EMPTY')
    if re.search('[A-Za-z]', prose) and not CYRILLIC.search(translated):
        errors.append('NO_CYRILLIC')
    if prose.strip() and source == target and re.search('[A-Za-z]', prose):
        errors.append('UNCHANGED')
    if len(target) > max(256, len(source) * 8):
        errors.append('SUSPICIOUS_LENGTH')
    if len(re.findall('[A-Za-z]', translated)) > max(8, len(CYRILLIC.findall(translated)) * 2):
        errors.append('MIXED_LANGUAGE')
    return errors


STRUCTURAL = frozenset(('MISSING', 'TOO_LARGE', 'UNICODE', 'TOKENS', 'EMPTY'))


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    detail: str = ''


class TranslationValidator:
    """M3 severity policy; legacy validate() remains the provider quality gate."""

    def check(self, source: str, target: str | None, *, proper_name: bool = False) -> list[Issue]:
        if not isinstance(source, str):
            return [Issue('ERROR', 'SOURCE_TYPE')]
        result = [Issue('ERROR' if code in STRUCTURAL else
                        ('INFO' if proper_name and code in ('UNCHANGED', 'NO_CYRILLIC', 'MIXED_LANGUAGE')
                         else 'WARNING'), code) for code in validate(source, target)]
        if isinstance(target, str):
            a, b = TOKEN.sub('', source).strip(), TOKEN.sub('', target).strip()
            if len(a) > 20 and 0 < len(b) < len(a) / 5:
                result.append(Issue('WARNING', 'SHORT'))
            if re.search(r'\b[A-Za-z]{3,}\b', b) and not proper_name:
                result.append(Issue('WARNING', 'ENGLISH_FRAGMENT'))
        return result


def blocking_errors(source: str, target: str | None) -> list[str]:
    return [i.code for i in TranslationValidator().check(source, target) if i.severity == 'ERROR']
