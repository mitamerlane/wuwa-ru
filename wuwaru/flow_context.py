"""Scenes, speakers and play order from the plain FlowLists. Context for translation, never a source of text.

The FlatBuffers layout is observed, not documented. Play order inside a chapter and branches remain UNKNOWN.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import struct

from .pak_reader import V12ArchiveReader
from .translation_db import TranslationDB

SHOW_TALK = 34
PLAYER = '{PlayerName}'
GENDERS = dict(FEMALE='F', MALE='M')
ADDRESSES = ('ты', 'вы')
NAME_FEMALE = re.compile(r'\b(?:Female|Woman|Girl|Lady|Madam|Mother|Sister|Grandma)\b')
NAME_MALE = re.compile(r'\b(?:Male|Man|Boy|Mister|Mr\.?|Father|Brother|Grandpa)\b')
SPEAKER_ID = re.compile(r'Speaker_(\d+)_Name')
DIALOGUE_FAMILY = re.compile(
    r'^(?:Flow|Main|MAIN|Side|SIDE|Character|Event|EVENT|POI|NPC|GNNPC|STNPC|QDHDNPCST|Huanglong\w*|Heihaian\w*|'
    r'Chengxiaoshan\w*|Shixifeidu\w*|zhuiyuejie|Zuoyequnxing|HD|CD|Daily|Daliy|phone|HRT|TWTPOI|LGNCZST|RGZYYYGJZ|'
    r'TZZNQ|BVBHD|StZLCZ|SZSTBC|HHACZST|DYHD|XFJCFST|SLDYS|Morale|ATYDZH|XJXYDTK|ktll\w*|YBWH)_.*\d$')
CLASS_PREFIXES = (
    ('SPEAKER', r'Speaker_'),
    ('QUEST', r'(?:Quest|LevelPlay)_\d|QuestSchedule|LongShanTask|AdventureTask|RogueEvent'),
    ('ITEM', r'ItemInfo|WeaponConf|WeaponBreach|Phantom|SynthesisFormula|Cook|Forge|Compose|Gacha|PayShop|Shop|Mail|Drop|Dango|Farm'),
    ('COMBAT', r'Skill|Term\d|RoleEvolve|ResonantChain|Rogue|RoverRogue|WeRouge|Babel|PropertyIndex|Property|Buff|Role[A-Z]|Element|Morph|WheelBattle|Pinball|Fishing|Trap'),
    ('LORE', r'FavorWord|FavorStory|FavorRoleInfo|FavorGoods|InfoDisplay|Achievement|MonsterInfo|MonsterHandBook|MonsterDetection|PhantomHandBook|ItemHandBook|Music|Handbook|PlotHandBook|Entity'),
    ('UI', r'PrefabTextItem|MenuConfig|ErrorCode|ConfirmBox|KeySetting|Text_|Guide|Tutorial|HelpText|Condition|MapMark|MapNote|InstanceDungeon|DungeonDetection|Area|AccessPath|LoadingTips|ComboTeaching|UiDynamicTab|HotKey|Generic|Common|Function|Activity|Report|Advice|Menu|Personal|Friend|Photo|BattlePass|Country|Influence|Occupation|Detection|Explore|Sneak'),
)
CLASS_RULES = [(name, re.compile('^(?:' + pattern + ')')) for name, pattern in CLASS_PREFIXES]


# In the order of the plan: names first because everything depends on them. 'scene' stands for every cell linked to a scene.
WAVES = dict(names=('speaker',), ui=('ui',), combat_items=('combat', 'item'), quests=('quest',),
             story=('scene', 'line'), other=('other',), lore=('lore',))


def text_kind(identifier: str) -> str:
    """What a cell outside any scene is, by its Id. A heuristic measured on the table, 'other' when unsure."""
    if DIALOGUE_FAMILY.match(identifier):
        return 'line'
    return next((name.lower() for name, rule in CLASS_RULES if rule.match(identifier)), 'other')


class Table:
    """Bounds-checked read access to one FlatBuffers table."""

    def __init__(self, data: bytes, offset: int):
        self.data, self.offset = data, offset
        self.vtable = offset - struct.unpack_from('<i', data, offset)[0]
        if not 0 <= self.vtable < len(data):
            raise ValueError('vtable вне файла')
        self.size = struct.unpack_from('<H', data, self.vtable)[0]

    def _at(self, slot: int) -> int:
        entry = 4 + 2 * slot
        if entry >= self.size:
            return 0
        field = struct.unpack_from('<H', self.data, self.vtable + entry)[0]
        return self.offset + field if field else 0

    def _target(self, at: int) -> int:
        target = at + struct.unpack_from('<I', self.data, at)[0]
        if target >= len(self.data):
            raise ValueError('Ссылка вне файла')
        return target

    def byte(self, slot: int) -> int | None:
        at = self._at(slot)
        return self.data[at] if at else None

    def integer(self, slot: int) -> int | None:
        at = self._at(slot)
        return struct.unpack_from('<i', self.data, at)[0] if at else None

    def text(self, slot: int) -> str | None:
        at = self._at(slot)
        if not at:
            return None
        target = self._target(at)
        size = struct.unpack_from('<I', self.data, target)[0]
        if target + 4 + size >= len(self.data):
            raise ValueError('Строка вне файла')
        return self.data[target + 4:target + 4 + size].decode('utf-8', 'replace')

    def table(self, slot: int) -> 'Table | None':
        at = self._at(slot)
        return Table(self.data, self._target(at)) if at else None

    def tables(self, slot: int) -> list['Table']:
        at = self._at(slot)
        if not at:
            return []
        start = self._target(at)
        count = struct.unpack_from('<I', self.data, start)[0]
        if start + 4 + 4 * count > len(self.data):
            raise ValueError('Вектор вне файла')
        return [Table(self.data, self._target(start + 4 + 4 * index)) for index in range(count)]


def flow_scenes(data: bytes) -> list[dict]:
    """Observed layout: list.2 flows, flow.11 states, state.7 actions, action.2 union; ShowTalk lines keep file order."""
    scenes = []
    for flow in Table(data, struct.unpack_from('<I', data, 0)[0]).tables(2):
        for state in flow.tables(11):
            for action in state.tables(7):
                wrapper = action.table(2)
                if wrapper is None or wrapper.byte(1) != SHOW_TALK or wrapper.table(2) is None:
                    continue
                lines = []
                for line in wrapper.table(2).tables(2):
                    key = line.text(3) or line.text(9)
                    if key:
                        lines.append(dict(key=key, who=line.integer(7), options=[
                            option.text(0) or option.text(3) for option in line.tables(13)
                            if option.text(0) or option.text(3)]))
                scenes.append(dict(flow=flow.integer(4), state=state.integer(4), flow_name=flow.text(5),
                                   state_name=state.text(5), lines=lines))
    return scenes


def stage_flows(game: Path, stage: Path) -> dict:
    """One verified copy per path; the patch overrides the base. Client load order itself remains UNKNOWN."""
    index_path = stage / 'index.json'
    if index_path.exists():
        return json.loads(index_path.read_text(encoding='utf-8'))
    reader = V12ArchiveReader(max_files=400_000, max_index_bytes=128 * 1024 * 1024,
                              max_metadata_bytes=512 * 1024 * 1024)
    archives = [game / 'Client/Content/Paks/pakchunk43-WindowsNoEditor.pak',
                *sorted(game.glob('Client/Saved/Resources/*/Resource/*/pakchunk43*.pak'))]
    stage.mkdir(parents=True, exist_ok=True)
    index = {}
    for archive in archives:
        with archive.open('rb') as stream:
            for entry in reader.read_metadata(archive, path_contains='flowlists/').entries:
                stream.seek(entry.offset + entry.header_size)
                payload = stream.read(entry.size)
                if hashlib.sha1(payload).hexdigest() != entry.sha1:
                    raise ValueError('SHA-1 не совпал: ' + entry.path)
                name = hashlib.sha256(entry.path.encode()).hexdigest()[:24] + '.bytes'
                (stage / name).write_bytes(payload)
                index[entry.path] = dict(file=name, size=entry.size, archive=archive.name, sha1=entry.sha1)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=0), encoding='utf-8')
    return index


def is_player(name: str) -> bool:
    return PLAYER in name or name == 'Rover'


def gender_evidence(name: str, candidates: dict) -> tuple[str | None, str | None]:
    """A candidate for a human to confirm, never a fact. No evidence stays unknown."""
    found = GENDERS.get((candidates.get(name) or {}).get('candidate'))
    if found:
        return found, 'pronouns'
    if NAME_FEMALE.search(name):
        return 'F', 'name'
    if NAME_MALE.search(name):
        return 'M', 'name'
    return None, None


def import_scenes(core: TranslationDB, scenes, *, sources: dict, candidates: dict, player_gender: str | None) -> dict:
    """Replace the scene map in one transaction. Records and human cast decisions stay as they are."""
    if player_gender not in (None, *GENDERS.values()):
        raise ValueError('Род игрока: F, M или неизвестен')
    known = {}
    for row in core.db.execute('SELECT seq,id_value FROM records WHERE active=1 ORDER BY seq'):
        known.setdefault(row['id_value'], row['seq'])
    speakers = {}
    for row in core.db.execute("SELECT id_value,source FROM records WHERE active=1 AND id_value LIKE 'Speaker_%_Name'"):
        if match := SPEAKER_ID.fullmatch(row['id_value']):
            speakers[int(match[1])] = row['source'].strip()
    lines, linked, missing, ordinal, numbered = Counter(), set(), 0, 0, 0
    with core.db:
        core.db.execute('BEGIN IMMEDIATE')
        core.db.execute('DELETE FROM scene_lines')
        core.db.execute('DELETE FROM scene_sources')
        core.db.executemany('INSERT INTO scene_sources(path,sha1,archive) VALUES (?,?,?)',
                            ((path, meta['sha1'], meta['archive']) for path, meta in sources.items()))
        for scene in scenes:
            numbered += 1
            for line in scene['lines']:
                name = speakers.get(line['who'], '')
                if name:
                    lines[name] += 1
                if line['options']:
                    lines[PLAYER] += len(line['options'])
                for key, kind, speaker in ((line['key'], 'line', name),
                                           *((option, 'choice', PLAYER) for option in line['options'])):
                    seq = known.get(key)
                    if seq is None:
                        missing += 1
                    elif seq not in linked:
                        linked.add(seq)
                        ordinal += 1
                        core.db.execute('INSERT INTO scene_lines(seq,ordinal,scene,kind,speaker) VALUES (?,?,?,?,?)',
                                        (seq, ordinal, numbered, kind, speaker or None))
        core.db.execute('UPDATE cast_members SET lines=0')
        for name, count in lines.items():
            gender, source = (player_gender, 'user') if is_player(name) and player_gender else gender_evidence(name, candidates)
            core.db.execute('INSERT INTO cast_members(name,gender,source,lines) VALUES (?,?,?,?) '
                            'ON CONFLICT(name) DO UPDATE SET lines=excluded.lines', (name, gender, source, count))
    return dict(scenes=numbered, linked=len(linked), missing=missing, speakers=len(lines), network_sent=False)


def set_cast_member(core: TranslationDB, name: str, *, gender: str | None = None, address: str | None = None,
                    approved: bool | None = None, source: str = 'human') -> None:
    """A human decision: later imports keep it, and the engine treats the new cast as a new snapshot."""
    if gender is not None and gender not in GENDERS.values():
        raise ValueError('Род: F или M')
    if address is not None and address not in ADDRESSES:
        raise ValueError('Обращение: ты или вы')
    before = core.db.execute('SELECT * FROM cast_members WHERE name=?', (name,)).fetchone()
    if before is None:
        raise ValueError('Такого говорящего нет в листе')
    with core.db:
        core.db.execute('UPDATE cast_members SET gender=?,address=?,source=?,approved=? WHERE name=?',
                        (gender or before['gender'], address or before['address'], source,
                         before['approved'] if approved is None else int(approved), name))


def cast_summary(core: TranslationDB) -> dict:
    row = core.db.execute('SELECT count(*),coalesce(sum(lines),0),coalesce(sum(approved),0),'
                          'coalesce(sum(CASE WHEN gender IS NULL THEN lines ELSE 0 END),0),'
                          'coalesce(sum(CASE WHEN address IS NULL THEN lines ELSE 0 END),0) FROM cast_members').fetchone()
    return dict(speakers=row[0], lines=row[1], approved=row[2], lines_without_gender=row[3], lines_without_address=row[4])
