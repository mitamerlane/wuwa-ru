"""Append-only, manually verified terminology. No blind prose substitution."""
from __future__ import annotations

from .translation_db import TranslationDB
from .validation import blocking_errors


def _english_forms(term: str) -> list[str]:
    """The written form first, then its base: the model inflects a hint, an exact-only match misses it."""
    forms = [term]
    bare = term.removesuffix("'s").removesuffix("'")
    forms.append(bare)
    if bare.endswith('es'):
        forms.append(bare[:-2])
    if bare.endswith('s'):
        forms.append(bare[:-1])
    return [form for form in forms if form]


class Glossary:
    def __init__(self, core: TranslationDB, *, version: int | None = None):
        self.core = core
        latest = self.core.db.execute('SELECT coalesce(max(revision),0) FROM glossary').fetchone()[0]
        if version is not None and (type(version) is not int or not 0 <= version <= latest):
            raise ValueError('Неизвестная версия словаря')
        self.pinned_version = version

    @property
    def version(self) -> int:
        if self.pinned_version is not None:
            return self.pinned_version
        return self.core.db.execute('SELECT coalesce(max(revision),0) FROM glossary').fetchone()[0]

    def add(self, term: str, russian: str, *, category: str, context: str = '',
            verified: bool = False) -> int:
        if self.pinned_version is not None:
            raise ValueError('Исторический snapshot словаря доступен только для чтения')
        if category not in ('character', 'location', 'ability', 'item', 'other', 'proper_name'):
            raise ValueError('Неизвестная категория термина')
        if not term or len(term) > 1024 or len(context) > 4096 or blocking_errors(term, russian):
            raise ValueError('Некорректный термин')
        with self.core.db:
            return self.core.db.execute('INSERT INTO glossary(term,context,russian,category,verified) '
                                        'VALUES (?,?,?,?,?)',
                                        (term, context, russian, category, int(verified))).lastrowid

    def exact(self, source: str, context: str) -> str | None:
        row = self.core.db.execute('SELECT russian FROM glossary WHERE term=? AND verified=1 AND revision<=? '
                                   "AND context IN ('',?) ORDER BY (context=?) DESC,revision DESC LIMIT 1",
                                   (source, self.version, context, context)).fetchone()
        return row[0] if row else None

    def hints(self, source: str, context: str) -> dict[str, str]:
        # Indexed lookup per lexical token; memory bounded by this string, not glossary size.
        import re
        if not self.version:
            return {}
        words = list(re.finditer(r"[\w'-]+", source))
        terms = [word[0] for word in words]
        hints = {}
        for size in range(1, min(6, len(terms)) + 1):
            for start in range(len(terms) - size + 1):
                term = ' '.join(terms[start:start + size])
                # The written span too: "Mt. Firmament" and "I.R.I.S." carry punctuation the word list drops.
                written = source[words[start].start():words[start + size - 1].end()]
                dotted = written + '.' if source[words[start + size - 1].end():].startswith('.') else written
                russian = None
                for form in dict.fromkeys((*_english_forms(term), dotted, written)):
                    russian = self.exact(form, context)
                    if russian is not None:
                        break
                if russian is not None:
                    hints[form if form in (dotted, written) else term] = russian
        return hints
