"""Small product-facing project helpers built on the existing translation store."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .core_contracts import ResourceSourceType
from .translation_db import TranslationDB


NATIVE_LIMITATIONS = (
    'Статус: UNKNOWN — совместимость с установленным клиентом не подтверждена.',
    'Версия клиента и активный набор ресурсов не установлены достоверно.',
    'Реальные языковые SQLite — недоступны; импортированный Public Textmap не является их заменой.',
    'Reader поддерживает ограниченное подмножество V12; большинство реальных индексов закрыты.',
    'Поддерживаемая точка загрузки пользовательского PAK и допуск ACE не подтверждены.',
    'Применение перед запуском проверено опытами REAPPLY и WAVES; совместимость с другими версиями клиента, риск для аккаунта и большинство экранов — UNKNOWN.',
)


def project_overview(core: TranslationDB) -> dict:
    """Return compact project counts and pinned public-source provenance."""
    db = core.db
    total, translated = db.execute(
        "SELECT count(*),sum(target IS NOT NULL AND translated_hash=source_hash "
        "AND status NOT IN ('INVALID','NEEDS_REVIEW')) FROM records WHERE active=1").fetchone()
    source_counts = dict(db.execute(
        'SELECT source_type,count(*) FROM records WHERE active=1 GROUP BY source_type'))
    resources = []
    for name, inspection in db.execute(
            'SELECT name,inspection FROM resources ORDER BY name'):
        try:
            metadata = json.loads(inspection)
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        resources.append(dict(name=name, source_type=metadata.get('source_type', 'SQLITE_STAGING'),
                              repository=metadata.get('repository'), revision=metadata.get('revision'),
                              data_version=metadata.get('data_version'), sha256=metadata.get('sha256')))
    public = [item for item in resources if item['source_type'] == 'PUBLIC_TEXTMAP']
    filled = translated or 0
    return dict(records=total, translated=filled,
                coverage_percent=round(100 * filled / total, 2) if total else 0,
                source_counts=source_counts, resources=resources, public_sources=public,
                installation_allowed=False)


def seed_mock_demo(workspace: Path) -> dict:
    """Create or preserve an isolated, resumable demo project with authored examples."""
    workspace = Path(workspace).absolute()
    demo = workspace / '.mock-demo'
    source = workspace / '.mock-demo-source.sqlite'
    if not source.exists():
        db = sqlite3.connect(source)
        try:
            db.execute('CREATE TABLE SyntheticText (Id TEXT PRIMARY KEY, Content TEXT)')
            db.executemany('INSERT INTO SyntheticText VALUES (?,?)', (
                ('demo-greeting', 'Welcome back, Rover!'),
                ('demo-quest', 'The signal is fading. Find the source.'),
                ('demo-token', 'Press {InputAction} to continue.'),
            ))
            db.commit()
        finally:
            db.close()
    core = TranslationDB(demo)
    try:
        existing = core.db.execute('SELECT count(*) FROM resources').fetchone()[0]
        if not existing:
            result = core.import_source(
                'demo/synthetic.db', source,
                source_type=ResourceSourceType.SYNTHETIC_FIXTURE,
                source_metadata={'purpose': 'PRODUCT-1 MockProvider demonstration',
                                 'translation': 'self-authored test text'})
        else:
            result = {'unchanged': core.db.execute(
                'SELECT count(*) FROM records WHERE active=1').fetchone()[0]}
        return dict(source_type='SYNTHETIC_FIXTURE', records=core.db.execute(
            'SELECT count(*) FROM records WHERE active=1').fetchone()[0], import_result=result)
    finally:
        core.close()
