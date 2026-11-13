"""«Играть»: one button launches Wuthering Waves through Steam with the Russian translation.

The page decides nothing about the game itself: `native_launch.inspect` gives the phase, the
pure functions below turn it into labels, and every write goes through the elevated helper.
Heavy steps (inspect, build, the helper) run in `EditorJob`; the GUI thread only reads the
4 KiB Mount and writes the small request file right before the helper starts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import re
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import native_helper, native_launch, native_package
from .native_package import table_label
from .native_launch import PAK_NAME, REBUILD_MISSING, RESOURCES, LaunchState, Phase
from .paths import inside
from .portable import DEFAULT_NATIVE_KINDS, NATIVE_KINDS
from .steam_art import find_hero

GROUND, TEXT, GOLD, CRIMSON, AMBER, SUCCESS = '#0B0D10', '#F5EFE2', '#E9B949', '#C23A3A', '#F2A35E', '#9FD8B8'
CREAM, MUTED, DIM, SOFT, ALERT = '#F5E6C4', '#C8C1B2', '#9C968A', '#E7DFCF', '#F08A80'
RISK = 'Неофициальный машинный перевод. Риск для аккаунта не оценён.'
ART_NOTE = 'Фон — обложка игры из кэша Steam, меняется вместе с событием'
BUNDLED_ART = 'background.jpg'
UAC_CANCELLED = 1223
HEADING = ['Unbounded', 'Segoe UI', 'Arial']  # bundled OFL fonts; the names fall back if loading fails
BODY = ['Noto Sans', 'Segoe UI', 'Arial']
FONT_FILES = ('Unbounded[wght].ttf', 'NotoSans[wdth,wght].ttf')
_fonts_loaded: bool | None = None


def register_fonts() -> bool:
    """Register the bundled OFL fonts once; the same path works frozen (PyInstaller datas)."""
    global _fonts_loaded
    if _fonts_loaded is None:
        folder = Path(__file__).parent / 'assets' / 'fonts'
        _fonts_loaded = all(QFontDatabase.addApplicationFont(str(folder / name)) >= 0 for name in FONT_FILES)
    return _fonts_loaded


KINDS = NATIVE_KINDS  # every kind text_kind can return, in WAVES order (see portable)
KIND_LABELS = {'speaker': 'Имена', 'ui': 'Интерфейс', 'combat': 'Бой и характеристики', 'item': 'Предметы',
               'quest': 'Задания', 'line': 'Диалоги', 'other': 'Прочее', 'lore': 'Справка и лор'}
WAVE_ROWS = (('Интерфейс и имена', ('speaker', 'ui')), ('Бой и характеристики', ('combat',)),
             ('Предметы', ('item',)), ('Диалоги и задания', ('quest', 'line')))


def kind_label(kind: str) -> str:
    label = f'{KIND_LABELS.get(kind, kind)} ({kind})'
    return label if kind in DEFAULT_NATIVE_KINDS else label + ' — в игре не проверено'


# ----------------------------------------------------------------------------------------------
# Pure decisions: phase → what the page shows. No Qt, unit-tested through the page.

@dataclass(frozen=True)
class Buttons:
    title: str
    subtitle: str
    enabled: bool
    tone: str = 'gold'               # 'gold' fills the play button, 'muted' is the dark variant
    alternative: str | None = None   # secondary action next to the play button
    recheck: bool = False
    table: bool = False              # «Получить таблицу»: the game's version is known, its table is not in place


@dataclass(frozen=True)
class Panel:
    title: str
    tone: str
    chip: tuple[str, str] | None = None
    count: str | None = None
    count_label: str = ''
    text: str = ''
    waves: tuple[tuple[str, bool], ...] = ()
    rows: tuple[tuple[str, str, str], ...] = ()
    note: str = ''
    left_out: tuple[str, str] = ('', '')


def has_installed_package(state: LaunchState) -> bool:
    package = state.package or {}
    return state.installed_sha256 is not None and state.installed_sha256 == package.get('pak_sha256')


def remove_block_reason(state: LaunchState | None) -> str | None:
    """Why remove cannot work although the game is reachable: the file under our PAK name matches
    neither package.json nor installed.json, so the helper would refuse the whole request."""
    if state is None or state.game is None or state.installed_sha256 is None:
        return None
    records = [record for record in (state.package, state.installed_record) if record is not None]
    if not records or state.installed_sha256 in {record.get('pak_sha256') for record in records}:
        return None  # without both records the helper keeps the PAK and removes only our line
    try:
        installed = native_launch.pak_path(Path(state.game))
    except (OSError, ValueError):
        installed = Path(state.game) / RESOURCES / (state.resources or '') / 'Lang_en/Base' / PAK_NAME
    return 'Перевод в игре не совпадает с записями приложения — удали файл вручную: ' + str(installed)


def can_remove(state: LaunchState | None) -> bool:
    """Same gate as `plan_remove`, and only when something of ours is there and the helper can act."""
    if (state is None or state.game is None or state.mount_sha256 is None
            or state.phase in (Phase.NO_GAME, Phase.GAME_RUNNING, Phase.STEAM_UPDATING)):
        return False
    if remove_block_reason(state) is not None:
        return False
    if state.package is None and state.installed_record is None:
        # No record names the file, so the helper would keep it: only our line is left to remove.
        return state.ours_in_mount
    return state.ours_in_mount or state.installed_sha256 is not None


def remove_deletes_pak(state: LaunchState) -> bool:
    """Whether `plan_remove` names exactly the file now in the game, so the helper deletes it."""
    if state.installed_sha256 is None or state.mount_sha256 is None:
        return False
    return native_launch.plan_remove(state)[0]['pak_sha256'] == state.installed_sha256


KEPT = {'kept: missing': 'Пакета в папке игры уже не было.',
        'kept: no package.json': 'Пакет не удалялся: у приложения нет записи о нём.',
        'kept: no hash in package.json': 'Пакет не удалялся: в записи приложения нет его SHA-256.'}


def remove_summary(operations: list[dict]) -> str:
    """What the helper really did, from result.json operations; never claims more."""
    line = any(op.get('op') == 'remove' and op.get('write') == 'mount' for op in operations)
    pak = next((op.get('pak') for op in operations if op.get('op') == 'remove' and op.get('pak')), None)
    parts = ['Наша строка убрана из списка пакетов.' if line else 'Нашей строки в списке пакетов не было.']
    if pak == 'removed':
        parts.append('Пакет перевода удалён из папки игры.')
    elif pak is not None:
        parts.append(KEPT.get(pak, f'Пакет не удалялся: {pak}.'))
    parts.append('В проекте перевод остаётся.')
    return ' '.join(parts)


def buttons_for(state: LaunchState | None) -> Buttons:
    if state is None:
        return Buttons('ПРОВЕРИТЬ СНОВА', '', True, 'muted')
    phase = state.phase
    if phase is Phase.READY:
        return Buttons('ИГРАТЬ', 'с русским переводом', True)
    if phase is Phase.NEEDS_INSTALL:
        return Buttons('ИГРАТЬ', 'установить перевод', True)
    if phase is Phase.NEEDS_REBUILD:
        return Buttons('СОБРАТЬ И ИГРАТЬ', '', True,
                       alternative='Играть со старым' if has_installed_package(state) else None)
    if phase is Phase.GAME_RUNNING:
        return Buttons('ИГРА ЗАПУЩЕНА', '', False, 'muted', recheck=True)
    if phase is Phase.STEAM_UPDATING:
        return Buttons('STEAM ОБНОВЛЯЕТ', '', False, 'muted', recheck=True)
    if phase is Phase.VERSION_CHANGED:
        accept = 'Принять версию на свой риск…' if state.fingerprint is not None else None
        table = bool(state.resources)
        if state.ours_in_mount:  # our old-table line is still mounted: take it out before Steam starts
            return Buttons('УБРАТЬ ПЕРЕВОД И ИГРАТЬ', '', can_remove(state), 'muted', accept, recheck=True,
                           table=table)
        return Buttons('ИГРАТЬ', 'без перевода', True, 'muted', accept, recheck=True, table=table)
    if phase is Phase.NO_GAME:
        return Buttons('Указать папку игры…', '', True, 'muted', recheck=True)
    return Buttons('Подключить входы…', '', True, 'muted', recheck=True)


def hint_for(state: LaunchState | None) -> str:
    phase = state.phase if state is not None else None
    if phase is Phase.VERSION_CHANGED and state.ours_in_mount:
        return 'Сначала перевод уберётся из игры, потом Steam откроет её без перевода'
    if phase is Phase.READY and state.mount_applied:
        return 'Строка перевода уже в списке пакетов: Steam откроется без запроса Windows'
    return {
        Phase.READY: 'Windows спросит разрешение на одну строку, потом откроется Steam',
        Phase.NEEDS_INSTALL: 'Windows спросит разрешение на пакет и одну строку, потом откроется Steam',
        Phase.NEEDS_REBUILD: 'Игра запустится сама, когда пакет будет готов',
        Phase.GAME_RUNNING: 'Закрой игру и лаунчер, потом нажми «Проверить снова»',
        Phase.STEAM_UPDATING: 'Дождись, пока Steam закончит обновление',
        Phase.VERSION_CHANGED: 'Steam откроет игру без перевода',
        Phase.NO_GAME: 'Укажи папку установки Wuthering Waves',
        Phase.NO_INPUTS: 'Нужны английская таблица и шрифт с закреплёнными SHA-256',
    }.get(phase, '')


LEFT_OUT = {'ENGLISH_DIFFERS': 'английский отличается от таблицы', 'MASKED': 'скрыта в таблице',
            'NOT_IN_TABLE': 'нет в таблице', 'DUPLICATE_ID': 'повтор ID', 'CHECK_FAILED': 'не прошла проверку',
            'NO_GLYPH': 'нет буквы в шрифте', 'STATUS_UNTRANSLATED': 'не переведено'}


def left_out_label(reason: str) -> str:
    if reason in LEFT_OUT:
        return LEFT_OUT[reason]
    if reason.startswith('STATUS_'):
        return 'статус ' + reason[len('STATUS_'):]
    return reason


def left_out_text(left_out) -> tuple[str, str]:
    """Compact line for the panel and the full list for its tooltip, largest reasons first."""
    if not isinstance(left_out, dict):
        return '', ''
    items = sorted(((reason, count) for reason, count in left_out.items() if isinstance(count, int) and count > 0),
                   key=lambda item: (-item[1], item[0]))
    if not items:
        return '', ''
    shown = ', '.join(f'{left_out_label(reason)} — {_number(count)}' for reason, count in items[:2])
    line = f'Не вошло {_number(sum(count for _, count in items))}: {shown}' + (', …' if len(items) > 2 else '')
    tip = 'Не вошло в пакет:\n' + '\n'.join(f'{left_out_label(reason)} — {_number(count)}'
                                             for reason, count in items)
    return line, tip


def _number(value) -> str:
    return f'{value:,}'.replace(',', ' ') if isinstance(value, int) else '—'


def megabytes(size: int) -> str:
    return f'{size / 1024 / 1024:.1f}'.replace('.', ',')


def panel_for(state: LaunchState | None, message: str = '') -> Panel:
    if state is None:
        return Panel('ПЕРЕВОД', DIM, text=message or 'Проверяю игру и пакет перевода…')
    package = state.package or {}
    built = set(package.get('kinds') or ())
    waves = tuple((name, bool(built) and set(kinds) <= built) for name, kinds in WAVE_ROWS)
    reasons = '\n'.join(state.reasons)
    resources = state.resources or '—'
    phase = state.phase
    if phase in (Phase.READY, Phase.NEEDS_INSTALL):
        chip = ('актуален', SUCCESS) if phase is Phase.READY else ('не установлен', AMBER)
        return Panel('ПЕРЕВОД', GOLD, chip, _number(package.get('cells')), 'строк на русском', waves=waves,
                     rows=(('Игра в Steam', 'найдена', TEXT), ('Пакет подходит к версии', 'да', SUCCESS),
                           ('Английская таблица', table_label(package.get('table')), TEXT),
                           ('Шрифт', 'Noto Sans 600', TEXT)), left_out=left_out_text(package.get('left_out')))
    if phase is Phase.NEEDS_REBUILD and REBUILD_MISSING in state.reasons:
        return Panel('ПЕРЕВОД', AMBER, ('пересборка', AMBER), _number(package.get('cells')), 'строк в пакете',
                     text='Пакета нет ни в папке игры, ни в Data/Native. Он соберётся заново из проекта '
                          'и установится, копий не остаётся.',
                     note='Отменить сборку можно в любой момент.', left_out=left_out_text(package.get('left_out')))
    if phase is Phase.NEEDS_REBUILD:
        return Panel('НОВЫЕ СТРОКИ', AMBER, ('пересборка', AMBER), _number(package.get('cells')), 'в пакете сейчас',
                     text='В проекте прибавился перевод. Пакет соберётся один раз и заменит старый в папке игры, '
                          'копий не остаётся.',
                     note='Старый пакет работает до конца сборки. Отменить можно в любой момент.')
    if phase is Phase.VERSION_CHANGED:
        if state.ours_in_mount:
            ours = ('строка ещё в списке пакетов', ALERT)
        elif state.installed_sha256 is not None:
            ours = ('лежит, не подключается', TEXT)
        else:
            ours = ('не установлен', TEXT)
        return Panel('ИГРА ОБНОВИЛАСЬ', CRIMSON,
                     text='Пакет собран под прежнюю версию. Старая таблица строк в новой игре опаснее английского, '
                          'поэтому перевод выключен до пересборки.',
                     rows=(('Набор ресурсов', resources, TEXT), ('Список языковых пакетов', 'изменился', ALERT),
                           ('Наш пакет', *ours), ('Английская таблица', table_label(state.table), TEXT)),
                     note='Нужна английская таблица новой версии: «Получить таблицу» возьмёт её из публичного '
                          'текста игры. С ней пакет пересоберётся, ручные правки сохранятся.')
    if phase is Phase.GAME_RUNNING:
        return Panel('ИГРА ЗАПУЩЕНА', AMBER,
                     text='Пока игра или лаунчер открыты, перевод не подключается и не удаляется.', note=reasons)
    if phase is Phase.STEAM_UPDATING:
        return Panel('STEAM ОБНОВЛЯЕТ ИГРУ', AMBER,
                     text='Steam качает или ставит обновление. Перевод подключится после него.', note=reasons)
    if phase is Phase.NO_GAME:
        return Panel('ИГРА НЕ НАЙДЕНА', AMBER,
                     text='Wuthering Waves в Steam не найдена. Укажи папку игры вручную.', note=reasons)
    return Panel('НЕТ ВХОДНЫХ ФАЙЛОВ', AMBER,
                 text='Для сборки нужны английская таблица lang_multi_text.en.db и шрифт NotoSans-600.ttf '
                      'с закреплёнными SHA-256.', note=reasons)


NO_SPACE = re.compile(r'Not enough free space: (\d+) bytes needed, (\d+) available')


def build_error(error: str) -> str:
    """The build's out-of-space OSError in Russian, with the same numbers; other errors as they are."""
    found = NO_SPACE.search(error)
    if found is None:
        return error
    need, free = (int(value) for value in found.groups())
    return (f'не хватает места в Data/Native: нужно {megabytes(need)} МБ, свободно {megabytes(free)} МБ. '
            'Освободи место на диске и нажми «Играть» снова')


TIMING_DEFAULTS = {'native-inspect': 4.0, 'native-build': 90.0}  # first run; then the last real time


def timed_fraction(elapsed: float, expected: float) -> float:
    """Progress that follows the clock: linear up to 90 % at 90 % of the expected time, then it
    slows down and never reaches 100 % before the step really ends."""
    ratio = max(elapsed, 0.0) / max(expected, 0.1)
    if ratio <= 0.9:
        return ratio
    return 0.99 - 0.09 * math.exp(-(ratio - 0.9) / 0.4)


def helper_ok(outcome: dict) -> bool:
    """Success needs both: exit code 0 and the helper's own result.json saying ok."""
    return outcome.get('code') == 0 and (outcome.get('result') or {}).get('ok') is True


def helper_error(outcome: dict) -> str:
    result = outcome.get('result')
    if result is None:
        return f"помощник не оставил результат (код {outcome.get('code')})"
    return result.get('error') or f"помощник завершился с кодом {outcome.get('code')}"


# ----------------------------------------------------------------------------------------------
# Painted widgets. Cut corners are QPainterPath, no images.

def font(families, size, weight=QFont.Normal) -> QFont:
    value = QFont()
    value.setFamilies(list(families))
    value.setPixelSize(size)
    value.setWeight(weight)
    return value


def cut_path(rect: QRectF, cut: float) -> QPainterPath:
    path = QPainterPath()
    path.moveTo(rect.left() + cut, rect.top())
    path.lineTo(rect.right(), rect.top())
    path.lineTo(rect.right(), rect.bottom() - cut)
    path.lineTo(rect.right() - cut, rect.bottom())
    path.lineTo(rect.left(), rect.bottom())
    path.lineTo(rect.left(), rect.top() + cut)
    path.closeSubpath()
    return path


def rgba(color: str, alpha: float) -> QColor:
    value = QColor(color)
    value.setAlphaF(alpha)
    return value


class Backdrop(QWidget):
    """Game cover scaled to cover, darkened to the right and at the bottom; plain ground without it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap: QPixmap | None = None

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(GROUND))
        if self.pixmap is not None and not self.pixmap.isNull():
            scaled = self.pixmap.scaled(rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = int((rect.width() - scaled.width()) * 0.28)
            painter.drawPixmap(x, (rect.height() - scaled.height()) // 2, scaled)
        side = QLinearGradient(rect.left(), 0, rect.right(), 0)
        for stop, alpha in ((0.3, 0.0), (0.58, 0.55), (0.8, 0.92), (1.0, 0.92)):
            side.setColorAt(stop, rgba(GROUND, alpha))
        painter.fillRect(rect, side)
        bottom = QLinearGradient(0, rect.bottom(), 0, rect.bottom() - 220)
        bottom.setColorAt(0, rgba(GROUND, 0.9))
        bottom.setColorAt(1, rgba(GROUND, 0.0))
        painter.fillRect(rect.adjusted(0, rect.height() - 220, 0, 0), bottom)


class CutPanel(QWidget):
    def __init__(self, parent=None, cut=18):
        super().__init__(parent)
        self.tone, self.cut, self.fill = GOLD, cut, rgba('#0C0E12', 0.8)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        painter.fillPath(cut_path(rect, self.cut), self.fill)
        painter.setPen(QPen(QColor(self.tone), 2))
        painter.drawLine(QPointF(rect.left() + self.cut, 1), QPointF(rect.right(), 1))


class Diamond(QWidget):
    def __init__(self, size=8, parent=None):
        super().__init__(parent)
        self.tone = GOLD
        self.setFixedSize(size + 4, size + 4)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        middle, half = self.width() / 2, self.width() / 2 - 1
        path = QPainterPath()
        path.moveTo(middle, middle - half)
        path.lineTo(middle + half, middle)
        path.lineTo(middle, middle + half)
        path.lineTo(middle - half, middle)
        path.closeSubpath()
        painter.fillPath(path, QColor(self.tone))


class Segments(QWidget):
    """Four wave segments: gold when the wave is in the package."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.done: list[bool] = []
        self.setFixedHeight(6)

    def paintEvent(self, event):
        if not self.done:
            return
        painter = QPainter(self)
        gap, count = 4, len(self.done)
        width = (self.width() - gap * (count - 1)) / count
        for index, done in enumerate(self.done):
            painter.fillRect(QRectF(index * (width + gap), 0, width, 6),
                             QColor(GOLD) if done else rgba(CREAM, 0.16))


class CutButton(QPushButton):
    """'ghost' outline or 'danger' crimson fill, cut corners."""

    def __init__(self, text='', kind='ghost', parent=None):
        super().__init__(text, parent)
        self.kind, self.cut = kind, 10
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(font(BODY, 15, QFont.DemiBold if kind == 'danger' else QFont.Normal))
        self.setMinimumHeight(52)

    def sizeHint(self):
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()) + 44, max(52, self.minimumHeight()))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = cut_path(rect, self.cut)
        painter.setOpacity(1.0 if self.isEnabled() else 0.45)
        if self.kind == 'danger':
            painter.fillPath(path, QColor(CRIMSON).lighter(115) if self.underMouse() else QColor(CRIMSON))
            painter.setPen(QColor('#FFF4EA'))
        else:
            painter.fillPath(path, rgba('#0C0E12', 0.85 if self.underMouse() else 0.7))
            painter.setPen(QPen(rgba(CREAM, 0.6 if self.underMouse() else 0.35), 1))
            painter.drawPath(path)
            painter.setPen(QColor(SOFT))
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignCenter, self.text())


class PlayButton(CutButton):
    """The big button: title plus a small subtitle, gold fill with a crimson bottom edge."""

    def __init__(self, parent=None):
        super().__init__('', 'gold', parent)
        self.cut, self.subtitle, self.tone = 16, '', 'gold'
        self.setMinimumSize(300, 72)

    def set_label(self, title: str, subtitle: str, tone: str) -> None:
        self.subtitle, self.tone = subtitle, tone
        self.setText(title)
        self.setAccessibleName(f'{title} {subtitle}'.strip())
        self.updateGeometry()
        self.update()

    def _title_font(self):
        return font(HEADING, 22 if len(self.text()) <= 16 else 17, QFont.Bold)

    def sizeHint(self):
        return QSize(max(300, QFontMetrics(self._title_font()).horizontalAdvance(self.text()) + 110), 72)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        path = cut_path(rect, self.cut)
        painter.setOpacity(1.0 if self.isEnabled() else 0.5)
        if self.tone == 'gold':
            fill = QColor(GOLD).lighter(108) if self.underMouse() and self.isEnabled() else QColor(GOLD)
            painter.fillPath(path, fill)
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(QRectF(0, rect.bottom() - 4, rect.width(), 4), QColor(CRIMSON))
            painter.restore()
            ink = QColor('#24170A')
        else:
            painter.fillPath(path, rgba('#181A20', 0.95 if self.underMouse() else 0.9))
            painter.setPen(QPen(rgba(CREAM, 0.5), 1))
            painter.drawPath(cut_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), self.cut))
            ink = QColor(CREAM)
        title_font = self._title_font()
        title_width = QFontMetrics(title_font).horizontalAdvance(self.text())
        icon = 'ИГРАТЬ' in self.text()
        block = title_width + (38 if icon else 0)
        left = (rect.width() - block) / 2
        if icon:
            triangle = QPainterPath()
            middle = rect.height() / 2
            triangle.moveTo(left, middle - 10)
            triangle.lineTo(left + 18, middle)
            triangle.lineTo(left, middle + 10)
            triangle.closeSubpath()
            painter.fillPath(triangle, ink)
            left += 38
        painter.setPen(ink)
        painter.setFont(title_font)
        if self.subtitle:
            painter.drawText(QRectF(left, 12, title_width + 4, 30), Qt.AlignLeft | Qt.AlignVCenter, self.text())
            painter.setFont(font(BODY, 12, QFont.DemiBold))
            painter.setOpacity(painter.opacity() * 0.8)
            painter.drawText(QRectF(left, 42, rect.width() - left, 18), Qt.AlignLeft | Qt.AlignVCenter, self.subtitle)
        else:
            painter.drawText(QRectF(left, 0, title_width + 4, rect.height()), Qt.AlignLeft | Qt.AlignVCenter,
                             self.text())


class RemoveButton(QToolButton):
    """Square 56×56 button with a drawn trash can."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(56, 56)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip('Удалить перевод')
        self.setAccessibleName('Удалить перевод')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.4)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = cut_path(rect, 10)
        painter.fillPath(path, rgba('#0C0E12', 0.85 if self.underMouse() else 0.7))
        painter.setPen(QPen(rgba(CRIMSON if self.underMouse() else CREAM, 0.6 if self.underMouse() else 0.35), 1))
        painter.drawPath(path)
        painter.translate(rect.center().x() - 10, rect.center().y() - 10)
        painter.scale(20 / 24, 20 / 24)
        can = QPainterPath()
        can.moveTo(4, 7)
        can.lineTo(20, 7)
        can.moveTo(9, 7)
        can.lineTo(9, 4)
        can.lineTo(15, 4)
        can.lineTo(15, 7)
        can.moveTo(6, 7)
        can.lineTo(7, 20)
        can.lineTo(17, 20)
        can.lineTo(18, 7)
        painter.setPen(QPen(QColor(SOFT), 1.8))
        painter.drawPath(can)


def _label(text='', size=14, color=TEXT, families=BODY, weight=QFont.Normal, wrap=True) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setFont(font(families, size, weight))
    label.setStyleSheet(f'color: {color}; background: transparent;')
    label.setWordWrap(wrap)
    return label


class RemoveDialog(QDialog):
    """Confirmation from the Remove mockup; nothing is written before Accepted."""

    def __init__(self, state: LaunchState, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Удалить перевод')
        self.setStyleSheet(f'QDialog {{ background: {GROUND}; }}')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        panel = CutPanel(cut=20)
        panel.tone, panel.fill = CRIMSON, rgba('#0E0F13', 0.94)
        panel.setMinimumWidth(520)
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(18)
        heading = QHBoxLayout()
        mark = Diamond(10)
        mark.tone = CRIMSON
        heading.addWidget(mark)
        heading.addWidget(_label('Удалить перевод из игры?', 22, TEXT, HEADING, QFont.DemiBold, wrap=False))
        heading.addStretch(1)
        layout.addLayout(heading)
        if remove_deletes_pak(state):
            size = state.installed_bytes  # the file in the game, not a newer build in package.json
            first = (f'Из папки игры уйдёт наш пакет, {megabytes(size)} МБ.' if isinstance(size, int)
                     else 'Из папки игры уйдёт наш пакет.')
        elif state.installed_sha256 is None:
            first = 'Нашего пакета в папке игры нет.'
        else:
            first = 'Файл пакета останется: у приложения нет записи о нём.'
        line = ('Из списка пакетов уйдёт наша строка.' if state.ours_in_mount
                else 'Нашей строки в списке пакетов нет.')
        bullets = QVBoxLayout()
        bullets.setSpacing(8)
        for text in (first, line,
                     'Оригинальные файлы игры не трогаются.',
                     'Перевод в проекте остаётся: «Играть» вернёт его за минуту.'):
            bullets.addWidget(_label('•  ' + text, 15, SOFT))
        layout.addLayout(bullets)
        layout.addWidget(_label('Игра и лаунчер должны быть закрыты. Windows спросит разрешение.', 14, '#B5AEA1'))
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.cancel_button = CutButton('Отмена')
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button = CutButton('Удалить перевод', 'danger')
        self.confirm_button.clicked.connect(self.accept)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.confirm_button)
        layout.addLayout(actions)
        self.cancel_button.setDefault(True)


# ----------------------------------------------------------------------------------------------

class PlayPageMixin:
    """EditorWindow part: the Play page, its states and the play / remove flows."""

    play_phase: Phase | None = None
    play_state: LaunchState | None = None
    play_installation: dict | None = None
    _play_pending = False
    ACTIVATION_DELAY_MS = 300  # after a dialog closes the flow's own job starts first; then this sees it

    def make_play_page(self) -> QWidget:
        register_fonts()
        page = Backdrop()
        page.setObjectName('playPage')
        self.play_page = page
        page.pixmap = self._play_background()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(40, 40, 40, 32)
        top = QHBoxLayout()
        top.addStretch(1)
        self.play_panel = CutPanel()
        self.play_panel.setFixedWidth(420)
        self.play_panel.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        top.addWidget(self.play_panel)
        outer.addLayout(top)
        outer.addStretch(1)

        panel = QVBoxLayout(self.play_panel)
        panel.setContentsMargins(28, 26, 28, 26)
        panel.setSpacing(16)
        heading = QHBoxLayout()
        self.play_mark = Diamond()
        heading.addWidget(self.play_mark)
        self.play_title = _label('ПЕРЕВОД', 16, TEXT, HEADING, QFont.DemiBold, wrap=False)
        heading.addWidget(self.play_title)
        heading.addStretch(1)
        self.play_chip = _label('', 13, SUCCESS, wrap=False)
        heading.addWidget(self.play_chip)
        panel.addLayout(heading)
        count = QHBoxLayout()
        self.play_count = _label('', 40, CREAM, HEADING, QFont.DemiBold, wrap=False)
        count.addWidget(self.play_count)
        self.play_count_label = _label('', 15, MUTED, wrap=False)
        count.addWidget(self.play_count_label, 0, Qt.AlignBottom)
        count.addStretch(1)
        panel.addLayout(count)
        self.play_text = _label('', 14, SOFT)
        panel.addWidget(self.play_text)
        self.play_segments = Segments()
        panel.addWidget(self.play_segments)
        self.play_waves = QVBoxLayout()
        self.play_waves.setSpacing(9)
        panel.addLayout(self.play_waves)
        self.play_rows = QVBoxLayout()
        self.play_rows.setSpacing(9)
        panel.addLayout(self.play_rows)
        self.play_progress = QProgressBar()
        self.play_progress.setRange(0, 1000)
        self.play_progress.setTextVisible(False)
        self.play_progress.setFixedHeight(6)
        self.play_progress.setAccessibleName('Готовность проверки')
        self.play_progress.setStyleSheet(f'QProgressBar {{ background: rgba(245,230,196,40); border: 0; }}'
                                         f'QProgressBar::chunk {{ background: {GOLD}; }}')
        self.play_progress.hide()
        panel.addWidget(self.play_progress)
        self._timed = None
        self._timed_timer = QTimer(page)
        self._timed_timer.setInterval(80)
        self._timed_timer.timeout.connect(self._timed_tick)
        self.play_left_out = _label('', 13, MUTED)
        self.play_left_out.setAccessibleName('Не вошло в пакет')
        panel.addWidget(self.play_left_out)
        self.play_note = _label('', 13, '#B5AEA1')
        panel.addWidget(self.play_note)

        footer = QHBoxLayout()
        footer.setSpacing(20)
        notes = QVBoxLayout()
        notes.setSpacing(4)
        notes.addStretch(1)
        self.play_art_note = _label(ART_NOTE, 12, DIM)
        self.play_art_note.setVisible(page.pixmap is not None)
        notes.addWidget(self.play_art_note)
        self.play_risk = _label(RISK, 12, DIM)
        notes.addWidget(self.play_risk)
        footer.addLayout(notes, 2)
        side = QVBoxLayout()
        side.setSpacing(12)
        side.addStretch(1)
        self.play_status = _label('', 14, SOFT)
        self.play_status.setAlignment(Qt.AlignRight)
        side.addWidget(self.play_status)
        self.play_hint = _label('', 14, SOFT)
        self.play_hint.setAlignment(Qt.AlignRight)
        side.addWidget(self.play_hint)
        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        self.play_recheck_button = CutButton('Проверить снова')
        self.play_recheck_button.setMinimumHeight(56)
        self.play_recheck_button.clicked.connect(lambda: self.refresh_play())
        buttons.addWidget(self.play_recheck_button)
        self.play_table_button = CutButton('Получить таблицу')
        self.play_table_button.setMinimumHeight(56)
        self.play_table_button.clicked.connect(self.fetch_table)
        buttons.addWidget(self.play_table_button)
        self.play_alt_button = CutButton()
        self.play_alt_button.setMinimumHeight(56)
        self.play_alt_button.clicked.connect(self.play_alternative)
        buttons.addWidget(self.play_alt_button)
        self.remove_button = RemoveButton()
        self.remove_button.clicked.connect(self.remove_translation)
        buttons.addWidget(self.remove_button)
        self.play_button = PlayButton()
        self.play_button.clicked.connect(self.play)
        buttons.addWidget(self.play_button)
        side.addLayout(buttons)
        footer.addLayout(side, 3)
        outer.addLayout(footer)
        self._play_busy('ПРОВЕРКА…')
        return page

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(self.ACTIVATION_DELAY_MS, self._refresh_on_activation)

    def _refresh_on_activation(self) -> None:
        """Back to the window (for example after the game ran): the client may have reset Mount,
        or the game may have closed. Only on the Play page, only when nothing runs and no dialog
        of ours or Windows' is in front."""
        if (getattr(self, 'play_page', None) is None or self.path_manager is None
                or getattr(self, 'job', None) is not None or not self.isActiveWindow()
                or QApplication.activeModalWidget() is not None
                or getattr(self, 'stack', None) is None or self.stack.currentWidget() is not self.play_page):
            return
        self.refresh_play()

    def _play_background(self) -> QPixmap | None:
        if self.path_manager is None:
            return None
        from .discovery import system_locations
        try:
            path = find_hero(system_locations()[0])
        except OSError:
            path = None
        if path is None:
            # the key art the installer put next to the program (never in Git), when Steam has none cached
            bundled = self.path_manager.root / BUNDLED_ART
            path = bundled if bundled.is_file() and not bundled.is_symlink() else None
        if path is None:
            return None
        pixmap = QPixmap(str(path))
        return None if pixmap.isNull() else pixmap

    # -- rendering ---------------------------------------------------------------------------

    def _fill(self, layout, rows):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget() is not None:
                        child.widget().deleteLater()
        for label, value, label_color, value_color, weight in rows:
            row = QHBoxLayout()
            row.addWidget(_label(label, 14, label_color, wrap=False))
            row.addStretch(1)
            row.addWidget(_label(value, 14, value_color, weight=weight, wrap=False))
            layout.addLayout(row)

    def _render_play(self, message: str = '') -> None:
        state = self.play_state
        spec, view = buttons_for(state), panel_for(state, message)
        available = self.path_manager is not None
        self.play_button.set_label(spec.title, spec.subtitle, spec.tone)
        self.play_button.setEnabled(spec.enabled and available)
        self.play_alt_button.setText(spec.alternative or '')
        self.play_alt_button.setVisible(spec.alternative is not None)
        self.play_alt_button.setEnabled(available)
        self.play_recheck_button.setVisible(spec.recheck or state is None)
        self.play_recheck_button.setEnabled(available)
        self.play_table_button.setVisible(spec.table)
        self.play_table_button.setEnabled(available)
        self.remove_button.setEnabled(available and can_remove(state))
        blocked = remove_block_reason(state)
        self.remove_button.setToolTip(blocked or 'Удалить перевод')
        self.play_hint.setText('\n'.join(text for text in (hint_for(state), blocked) if text))
        self.play_progress.hide()
        self.play_panel.tone = view.tone
        self.play_mark.tone = view.tone
        self.play_title.setText(view.title)
        self.play_chip.setText(view.chip[0] if view.chip else '')
        self.play_chip.setStyleSheet(f'color: {view.chip[1] if view.chip else TEXT}; background: transparent;')
        self.play_count.setText(view.count or '')
        self.play_count.setVisible(view.count is not None)
        self.play_count_label.setText(view.count_label)
        self.play_count_label.setVisible(view.count is not None)
        self.play_text.setText(view.text)
        self.play_text.setVisible(bool(view.text))
        self.play_segments.done = [done for _, done in view.waves]
        self.play_segments.setVisible(bool(view.waves))
        self.play_segments.update()
        self._fill(self.play_waves, [(name, 'готово' if done else 'в очереди', TEXT if done else MUTED,
                                      GOLD if done else DIM, QFont.DemiBold if done else QFont.Normal)
                                     for name, done in view.waves])
        self._fill(self.play_rows, [(label, value, MUTED, color, QFont.Normal) for label, value, color in view.rows])
        self.play_left_out.setText(view.left_out[0])
        self.play_left_out.setToolTip(view.left_out[1])
        self.play_left_out.setVisible(bool(view.left_out[0]))
        self.play_note.setText(view.note)
        self.play_note.setVisible(bool(view.note))
        self.play_panel.update()
        self.play_mark.update()

    # -- progress paced by the clock ------------------------------------------------------------

    def _timed_start(self, action: str, title: str, suffix: str = '') -> None:
        """Show percent on the play button and the bar; paced by how long `action` took last time."""
        self._timed_finish(ok=True)
        expected = TIMING_DEFAULTS.get(action, 5.0)
        if self.path_manager is not None:
            expected = self.path_manager.timing(action, expected)
        self._timed = (action, title, suffix, time.monotonic(), expected)
        self.play_progress.setValue(0)
        self.play_progress.show()
        self._timed_tick()
        self._timed_timer.start()

    def _timed_tick(self) -> None:
        if self._timed is None:
            return
        _, title, suffix, started, expected = self._timed
        fraction = timed_fraction(time.monotonic() - started, expected)
        self.play_progress.setValue(round(fraction * 1000))
        self.play_button.set_label(title, f'{int(fraction * 100)}%' + suffix,
                                   'gold' if self.play_button.tone == 'gold' else 'muted')

    def _timed_finish(self, ok: bool) -> None:
        """Stop the bar; a step that really finished teaches the next run how long it takes."""
        if self._timed is None:
            return
        action, _, _, started, expected = self._timed
        self._timed = None
        self._timed_timer.stop()
        self.play_progress.setValue(1000)
        self.play_progress.hide()
        if ok and self.path_manager is not None:
            elapsed = time.monotonic() - started
            try:
                self.path_manager.save_timing(action, (expected + 2 * elapsed) / 3)
            except OSError:
                pass

    def _play_busy(self, title: str, subtitle: str = '') -> None:
        self.play_button.set_label(title, subtitle, 'gold' if self.play_button.tone == 'gold' else 'muted')
        for widget in (self.play_button, self.play_alt_button, self.play_recheck_button, self.play_table_button,
                       self.remove_button):
            widget.setEnabled(False)

    def _play_args(self, **extra) -> dict:
        installation = self.play_installation or {}
        game = installation.get('path') or self.path_manager.game_path()
        return dict(native=str(self.path_manager.native), game=str(game) if game else None,
                    build_id=installation.get('steam_build_id') if installation.get('path') else None,
                    project_db=str(self.workspace / 'translations.sqlite'),
                    kinds=list(self.path_manager.native_kinds()), **extra)

    # -- flows -------------------------------------------------------------------------------

    def refresh_play(self, then: str | None = None, key: str | None = None) -> None:
        """Re-inspect in EditorJob. `then` continues a flow: 'play' after a build, 'old' for the old package."""
        if self.path_manager is None:
            self.play_state, self.play_phase = None, None
            self._render_play('Страница «Играть» работает в portable-режиме: запусти приложение из папки '
                              'WuWaRussian, где лежат Data и Projects.')
            self.play_status.setText('Нужна portable-папка: без неё перевод в игру не подключается.')
            return
        if self.job is not None:
            self._play_pending = True
            return
        self._play_busy('ПРОВЕРКА…')
        self._timed_start('native-inspect', 'ПРОВЕРКА…')
        args = self._play_args(**({'key': key} if key else {}))
        self.task('native-inspect', args, lambda state: self.show_play_state(state, then))

    def show_play_state(self, state: LaunchState, then: str | None = None) -> None:
        """`then` continues a click on the state inspected just now: 'click' (Играть), 'remove' /
        'remove-launch' (Удалить перевод), 'play' after a build, 'old' for the installed package."""
        self._timed_finish(ok=True)
        self.play_state, self.play_phase = state, state.phase
        self._render_play()
        if then == 'click':
            self._play_on(state)
        elif then in ('remove', 'remove-launch'):
            self._remove_on(state, launch=then == 'remove-launch')
        elif then == 'play' and state.phase in (Phase.READY, Phase.NEEDS_INSTALL):
            self._apply_play(state)
        elif then == 'old' and state.phase is Phase.READY:
            self._apply_play(state)
        elif then is not None:
            self.play_status.setText('Состояние игры изменилось, запуск отменён. Проверь и нажми снова.')

    def play_failed(self, action: str, error: str) -> None:
        prefix = {'native-inspect': 'Проверка не удалась', 'native-build': 'Сборка не завершена',
                  'native-apply': 'Помощник не запустился', 'native-inputs': 'Входы не подключены',
                  'native-table': 'Таблица не получена'}
        self._timed_finish(ok=False)
        if action == 'native-build':
            error = build_error(error)
        self.play_status.setText(f"{prefix.get(action, 'Ошибка')}: {error}")
        if action == 'native-inspect':
            self.play_state, self.play_phase = None, None
            self._render_play(error)
        else:
            self.refresh_play()

    def play(self) -> None:
        """Always acts on a fresh inspect: the client resets Mount after start, the game may be closed."""
        if self.path_manager is None:
            return
        if self.job is not None:
            self.play_status.setText('Подожди: идёт другая операция.')
            return
        self.play_status.setText('')
        self.refresh_play(then='click')

    def _play_on(self, state: LaunchState) -> None:
        phase = state.phase
        if phase in (Phase.READY, Phase.NEEDS_INSTALL):
            self._apply_play(state)
        elif phase is Phase.NEEDS_REBUILD:
            self._play_busy('СБОРКА…', 'отменить — Esc')
            self._timed_start('native-build', 'СБОРКА…', ' · отменить — Esc')
            self.task('native-build', self._play_args(), lambda report: self.refresh_play(then='play'))
        elif phase is Phase.VERSION_CHANGED and state.ours_in_mount:
            self._remove_on(state, launch=True)
        elif phase is Phase.VERSION_CHANGED:
            self._launch_steam('Steam открывает игру без перевода.')
        elif phase is Phase.NO_GAME:
            self.choose_game_folder()
        elif phase is Phase.NO_INPUTS:
            self.connect_native_inputs()

    def play_alternative(self) -> None:
        state = self.play_state
        if self.job is not None or state is None:
            return
        if state.phase is Phase.NEEDS_REBUILD and has_installed_package(state):
            # «Играть со старым»: re-inspect with the installed package's key; READY then plans apply_mount only.
            self.refresh_play(then='old', key=state.package['key'])
        elif state.phase is Phase.VERSION_CHANGED:
            self.accept_version()

    def fetch_table(self) -> None:
        """«Получить таблицу»: the English table of this game version, built from the public text source."""
        state = self.play_state
        if self.job is not None or state is None or state.phase is not Phase.VERSION_CHANGED or not state.resources:
            return
        self._play_busy('ТАБЛИЦА…')
        self.play_status.setText(f'Беру английскую таблицу версии {state.resources} из публичного текста игры, '
                                 'около 50 МБ…')
        self.task('native-table', dict(native=str(self.path_manager.native), game_version=state.resources),
                  self._table_taken)

    def _table_taken(self, result: dict) -> None:
        label = table_label(result['record'])
        self.refresh_play()
        self.play_status.setText(f'Таблица получена: {label}. Прими версию на свой риск, и пакет соберётся на ней.'
                                 if result['changed'] else
                                 f'Таблица уже этой версии: {label}. Осталось принять версию на свой риск.')

    def accept_version(self) -> None:
        state = self.play_state
        if state is None or state.fingerprint is None:
            return
        native = self.path_manager.native
        try:
            table = table_label(native_package.table_record(native))
            reused = native_launch.table_reused(native, state.fingerprint)
        except (OSError, ValueError) as exc:
            self.play_status.setText(f'Таблица строк не прочитана: {exc}')
            return
        stale = ('Эта таблица уже использовалась с прежней версией игры. Наш пакет подменяет таблицу строк '
                 'целиком, поэтому новые официальные строки этой версии останутся скрытыми или старыми. '
                 'Сначала возьми свежую таблицу кнопкой «Получить таблицу».\n\n' if reused else '')
        answer = QMessageBox.question(
            self, 'Принять версию на свой риск',
            'Эта версия игры не проверена. Совместимость перевода с ней и риск для аккаунта: UNKNOWN.\n\n'
            f'Пакет соберётся на английской таблице {table}.\n\n{stale}'
            'Согласие записывается в журнал и действует только для этого списка языковых пакетов и этой таблицы. '
            'Убрать перевод можно в любой момент кнопкой «Удалить перевод».\n\nПринять версию на свой риск?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            native_launch.accept_fingerprint(self.path_manager.native, state.fingerprint)
        except (OSError, ValueError) as exc:
            self.play_status.setText(f'Согласие не записано: {exc}')
            return
        self.play_status.setText('Версия принята на твой риск.')
        self.refresh_play()

    def _launch_steam(self, done: str) -> bool:
        try:
            native_helper.launch_steam()
        except OSError as exc:
            self.play_status.setText(f'Steam не открылся: {exc}')
            return False
        self.play_status.setText(done)
        return True

    def _request(self, state: LaunchState, plan_for) -> tuple[list[dict], Path] | None:
        """Plan from the inspected state and write the request with the Mount bytes read just now."""
        native = self.path_manager.native
        try:
            mount = native_launch.read_bounded(native_launch.mount_path(state.game))
            plan = plan_for(state)
            return plan, native_helper.write_request(native, state.game, plan, mount)
        except (OSError, ValueError) as exc:
            self.play_status.setText(f'Запись отменена: {exc}')
            self.refresh_play()
            return None

    def _apply_play(self, state: LaunchState) -> None:
        if state.phase is Phase.READY and state.mount_applied:
            self._launch_steam('Игра запущена — перевод уже подключён, разрешение Windows не понадобилось')
            return
        prepared = self._request(state, lambda value: native_launch.plan_play(value, self.path_manager.native))
        if prepared is None:
            return
        plan, request = prepared
        self._play_busy('ЗАПУСК…')
        self.play_hint.setText('Проверка · строка в списке пакетов · Steam')
        self.task('native-apply', dict(native=str(self.path_manager.native), request=str(request)),
                  lambda outcome: self.play_applied(outcome, plan))

    def play_applied(self, outcome: dict, plan: list[dict]) -> None:
        code = outcome['code']
        if helper_ok(outcome):
            self._record_install(plan)
            native_launch.drop_staging(self.path_manager.native, plan)
            self._launch_steam('Игра запущена — перевод подключится при старте клиента')
        elif code == UAC_CANCELLED:
            self.play_status.setText('Без разрешения перевод не подключится')
        else:
            self.play_status.setText('Перевод не подключён: ' + helper_error(outcome))
        self.refresh_play()

    def _record_install(self, plan: list[dict]) -> None:
        """installed.json names the PAK now in the game, so remove keeps working after the next build."""
        for op in plan:
            if op['op'] == 'install_package':
                try:
                    native_launch.record_installed(self.path_manager.native, op)
                except OSError as exc:
                    self.play_status.setText(f'Не записан installed.json: {exc}')

    def remove_translation(self, launch: bool = False) -> None:
        """Re-inspect, then confirm and remove through the helper on that fresh state."""
        if self.path_manager is None:
            return
        if self.job is not None:
            self.play_status.setText('Подожди: идёт другая операция.')
            return
        self.play_status.setText('')
        self.refresh_play(then='remove-launch' if launch else 'remove')

    def _remove_on(self, state: LaunchState, launch: bool = False) -> None:
        """`launch` opens Steam only after a verified success."""
        if not can_remove(state):
            self.play_status.setText(remove_block_reason(state) or 'Удалять нечего: перевода в игре нет.')
            return
        if RemoveDialog(state, self).exec() != QDialog.Accepted:
            return
        prepared = self._request(state, native_launch.plan_remove)
        if prepared is None:
            return
        _, request = prepared
        self._play_busy('УДАЛЕНИЕ…')
        self.task('native-apply', dict(native=str(self.path_manager.native), request=str(request)),
                  lambda outcome: self.remove_applied(outcome, launch))

    def remove_applied(self, outcome: dict, launch: bool = False) -> None:
        code = outcome['code']
        if helper_ok(outcome):
            summary = remove_summary(outcome['result'].get('operations') or [])
            self.play_status.setText(summary)
            if launch:
                self._launch_steam(summary + ' Steam открывает игру без перевода.')
        elif code == UAC_CANCELLED:
            self.play_status.setText('Без разрешения перевод не удалится')
        else:
            self.play_status.setText('Перевод не удалён: ' + helper_error(outcome))
        self.refresh_play()

    def choose_game_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, 'Папка Wuthering Waves (только чтение)')
        if not path:
            return
        from .discovery import inspect_installation
        try:
            installation = inspect_installation(Path(path))
        except (OSError, ValueError) as exc:
            self.play_status.setText(str(exc))
            return
        self.path_manager.save_game_path(installation.root)
        self.play_installation = None
        self.discover_game()
