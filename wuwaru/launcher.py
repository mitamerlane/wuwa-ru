"""The player's window: frameless and compact, only «Играть» and translation settings.

The translator's tools (editor, QA, providers, export) stay in `EditorWindow`, opened with
`--editor`. This window reuses the Play page (`PlayPageMixin`) and the same `EditorJob` worker;
it has no store or flow of its own.
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QUrl, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout,
                               QWidget)

from . import __version__
from .editor_worker import EditorJob
from .play_page import (BODY, CREAM, DIM, GOLD, GROUND, HEADING, KINDS, MUTED, SOFT, TEXT, CutButton, CutPanel,
                        PlayPageMixin, _label, can_remove, font, kind_label, register_fonts, rgba)
from .portable import PathManager
from .delta_ui import DeltaUpdateMixin
from .update_ui import UpdateMixin

WIDTH, HEIGHT = 1040, 640
TITLE_HEIGHT = 52
ICON = Path(__file__).parent / 'assets' / 'icons' / 'app.ico'


def app_icon() -> QIcon:
    return QIcon(str(ICON)) if ICON.is_file() else QIcon()


class WindowButton(QPushButton):
    """Minimize / close drawn as lines: no dependence on a font having ✕."""

    def __init__(self, kind: str, tip: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setObjectName(kind)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(46, 36)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        hot = self.underMouse()
        if hot:
            painter.fillRect(self.rect(), QColor('#C23A3A') if self.kind == 'close' else rgba(CREAM, 0.1))
        painter.setPen(QPen(QColor('#FFF4EA' if hot else MUTED), 1.4))
        x, y = self.width() / 2, self.height() / 2
        if self.kind == 'close':
            painter.drawLine(QPointF(x - 6, y - 6), QPointF(x + 6, y + 6))
            painter.drawLine(QPointF(x - 6, y + 6), QPointF(x + 6, y - 6))
        else:
            painter.drawLine(QPointF(x - 6, y), QPointF(x + 6, y))


class TitleBar(QWidget):
    """Brand, the two sections, minimize and close; dragging it moves the window (with Snap)."""

    def __init__(self, window: 'LauncherWindow'):
        super().__init__(window)
        self.window_ = window
        self.setObjectName('titleBar')
        self.setFixedHeight(TITLE_HEIGHT)
        row = QHBoxLayout(self)
        row.setContentsMargins(24, 0, 8, 0)
        row.setSpacing(6)
        brand = QLabel()
        brand.setTextFormat(Qt.RichText)
        brand.setText(f'<span style="color:{TEXT}">WuWa </span><span style="color:{GOLD}">Russian</span>')
        brand.setFont(font(HEADING, 15, QFont.DemiBold))
        row.addWidget(brand)
        row.addSpacing(22)
        self.sections = []
        for index, text in enumerate(('Играть', 'Настройки')):
            button = QPushButton(text)
            button.setObjectName('section')
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setFont(font(BODY, 14, QFont.DemiBold))
            button.clicked.connect(lambda checked=False, index=index: window.show_section(index))
            row.addWidget(button)
            self.sections.append(button)
        row.addStretch(1)
        for kind, tip, slot in (('minimize', 'Свернуть', window.showMinimized), ('close', 'Закрыть', window.close)):
            button = WindowButton(kind, tip)
            button.clicked.connect(slot)
            row.addWidget(button)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.window_.windowHandle() is not None:
            self.window_.windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), rgba('#08090C', 0.92))
        painter.setPen(QPen(rgba(GOLD, 0.25), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)


class Frame(QWidget):
    """Window body: ground colour and a thin outline, since the system frame is gone."""

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(GROUND))


class Outline(QWidget):
    """Transparent overlay that draws the window outline above every page."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(QPen(rgba(GOLD, 0.30), 1))
        painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))


class LauncherWindow(UpdateMixin, DeltaUpdateMixin, PlayPageMixin, QWidget):
    def __init__(self, workspace: Path, path_manager: PathManager | None = None):
        super().__init__()
        register_fonts()
        self.workspace = Path(workspace)
        self.path_manager = path_manager
        self.job = None
        self.last_error = ''
        self._result = None
        self._active_action = ''
        self._callback = None
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowMinimizeButtonHint
                            | Qt.WindowSystemMenuHint)
        self.setWindowTitle('WuWa Russian')
        self.setWindowIcon(app_icon())
        self.setFixedSize(WIDTH, HEIGHT)
        self.setStyleSheet(f'''
            QWidget {{ color: {TEXT}; }}
            #titleBar QPushButton {{ background: transparent; border: 0; color: {MUTED}; font-size: 15px; }}
            #titleBar QPushButton#section {{ padding: 14px 14px 12px; border-bottom: 2px solid transparent;
                                            font-size: 14px; }}
            #titleBar QPushButton#section:hover {{ color: {CREAM}; }}
            #titleBar QPushButton#section:checked {{ color: {CREAM}; border-bottom: 2px solid {GOLD}; }}
            #playPage QWidget, #settingsPage QWidget {{ background: transparent; }}
            QCheckBox {{ color: {SOFT}; font-size: 14px; spacing: 10px; }}
            QCheckBox::indicator {{ width: 18px; height: 18px; border: 1px solid rgba(245,230,196,90);
                                   background: rgba(0,0,0,90); }}
            QCheckBox::indicator:checked {{ background: {GOLD}; border-color: {GOLD}; }}
            QCheckBox:disabled {{ color: {DIM}; }}
            QToolTip {{ background: #12151A; color: {SOFT}; border: 1px solid rgba(233,185,73,90); }}
        ''')
        body = Frame(self)
        body.setGeometry(0, 0, WIDTH, HEIGHT)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.title_bar = TitleBar(self)
        layout.addWidget(self.title_bar)
        self.stack = QStackedWidget()
        self.play_page_widget = self.make_play_page()
        self._compact_play_page()
        self.make_delta_status()
        self.stack.addWidget(self.play_page_widget)
        self.settings_page = self.make_launcher_settings()
        self.stack.addWidget(self.settings_page)
        layout.addWidget(self.stack, 1)
        Outline(body).setGeometry(0, 0, WIDTH, HEIGHT)
        escape = QAction(self)
        escape.setShortcut(QKeySequence('Escape'))
        escape.triggered.connect(self.cancel)
        self.addAction(escape)
        self.show_section(0)
        if self.path_manager is not None:
            self.discover_game()
        self.refresh_play()
        self.schedule_update_check()
        self.schedule_delta_update()

    # -- layout --------------------------------------------------------------------------------

    def _compact_play_page(self) -> None:
        """The Play page was laid out for a large window: tighter margins and panel here."""
        self.play_page.layout().setContentsMargins(28, 24, 28, 22)
        self.play_panel.setFixedWidth(400)
        self.play_panel.layout().setContentsMargins(24, 20, 24, 20)
        self.play_panel.layout().setSpacing(12)

    def show_section(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for position, button in enumerate(self.title_bar.sections):
            button.setChecked(position == index)
        if index == 1:
            self.update_settings()

    def make_launcher_settings(self) -> QWidget:
        page = QWidget()
        page.setObjectName('settingsPage')
        outer = QHBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(20)

        waves = CutPanel()
        column = QVBoxLayout(waves)
        column.setContentsMargins(26, 22, 26, 22)
        column.setSpacing(10)
        column.addWidget(_label('ЧТО ПЕРЕВОДИТЬ', 16, TEXT, HEADING, QFont.DemiBold, wrap=False))
        column.addWidget(_label('Имена и интерфейс проверены в игре. Остальное — на свой риск; '
                                'изменение пересоберёт перевод при следующем «Играть».', 13, MUTED))
        column.addSpacing(4)
        chosen = self.path_manager.native_kinds() if self.path_manager is not None else ()
        self.native_kind_boxes = {}
        for kind in KINDS:
            box = QCheckBox(re.sub(r' \(\w+\)', '', kind_label(kind)))  # no internal code
            box.setChecked(kind in chosen)
            box.setEnabled(self.path_manager is not None)
            box.setCursor(Qt.PointingHandCursor)
            box.toggled.connect(lambda checked=False: self.save_native_kinds())
            self.native_kind_boxes[kind] = box
            column.addWidget(box)
        column.addStretch(1)
        outer.addWidget(waves, 1)

        side = QVBoxLayout()
        side.setSpacing(20)
        game = CutPanel()
        game_layout = QVBoxLayout(game)
        game_layout.setContentsMargins(26, 22, 26, 22)
        game_layout.setSpacing(10)
        game_layout.addWidget(_label('ИГРА', 16, TEXT, HEADING, QFont.DemiBold, wrap=False))
        self.settings_game = _label('', 13, SOFT)
        self.settings_game.setTextInteractionFlags(Qt.TextSelectableByMouse)
        game_layout.addWidget(self.settings_game)
        row = QHBoxLayout()
        choose = CutButton('Указать папку игры…')
        choose.setMinimumHeight(44)
        choose.clicked.connect(self.choose_game_folder)
        row.addWidget(choose)
        row.addStretch(1)
        game_layout.addLayout(row)
        side.addWidget(game)

        remove = CutPanel()
        remove.tone = '#C23A3A'
        remove_layout = QVBoxLayout(remove)
        remove_layout.setContentsMargins(26, 22, 26, 22)
        remove_layout.setSpacing(10)
        remove_layout.addWidget(_label('ПЕРЕВОД В ИГРЕ', 16, TEXT, HEADING, QFont.DemiBold, wrap=False))
        remove_layout.addWidget(_label('Убирает наш пакет и строку из папки игры. Оригинальные файлы '
                                       'не трогаются; «Играть» вернёт перевод.', 13, MUTED))
        row = QHBoxLayout()
        self.settings_remove = CutButton('Убрать перевод из игры', 'danger')
        self.settings_remove.setMinimumHeight(44)
        self.settings_remove.clicked.connect(self.remove_from_settings)
        row.addWidget(self.settings_remove)
        row.addStretch(1)
        remove_layout.addLayout(row)
        side.addWidget(remove)
        side.addStretch(1)
        footer = QHBoxLayout()
        folder = CutButton('Папка данных')
        folder.setMinimumHeight(40)
        folder.setEnabled(self.path_manager is not None)
        folder.clicked.connect(self.open_data_folder)
        footer.addWidget(folder)
        self.update_button = CutButton('Проверить обновления')
        self.update_button.setMinimumHeight(40)
        self.update_button.clicked.connect(lambda: self.check_updates(quiet=False))
        footer.addWidget(self.update_button)
        footer.addStretch(1)
        footer.addWidget(_label(f'Версия {__version__}', 12, DIM, wrap=False), 0, Qt.AlignBottom)
        side.addLayout(footer)
        self.update_status = _label('', 12, DIM)
        side.addWidget(self.update_status)
        outer.addLayout(side, 1)
        return page

    def update_settings(self) -> None:
        installation = self.play_installation or {}
        game = installation.get('path') or (self.path_manager.game_path() if self.path_manager else None)
        text = str(game) if game else 'Папка Wuthering Waves не найдена.'
        self.settings_game.setToolTip(text)
        self.settings_game.setText(self.settings_game.fontMetrics().elidedText(text, Qt.ElideMiddle, 420))
        self.settings_remove.setEnabled(self.job is None and can_remove(self.play_state))

    # -- actions -------------------------------------------------------------------------------

    def save_native_kinds(self) -> None:
        if self.path_manager is None:
            return
        self.path_manager.save_native_kinds(kind for kind in KINDS if self.native_kind_boxes[kind].isChecked())
        self.refresh_play()

    def remove_from_settings(self) -> None:
        self.show_section(0)
        self.remove_translation()

    def open_data_folder(self) -> None:
        if self.path_manager is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path_manager.root)))

    def connect_native_inputs(self) -> None:
        if self.path_manager is None or self.job is not None:
            return
        table, _ = QFileDialog.getOpenFileName(
            self, 'Английская таблица lang_multi_text.en.db (закреплённый snapshot)', '', 'SQLite (*.db)')
        if not table:
            return
        font_path, _ = QFileDialog.getOpenFileName(self, 'Шрифт NotoSans-600.ttf', '', 'TrueType (*.ttf)')
        if not font_path:
            return
        self.task('native-inputs', dict(native=str(self.path_manager.native), table=table, font=font_path),
                  self.native_inputs_connected)

    def native_inputs_connected(self, result) -> None:
        error = (result or {}).get('error')
        self.play_status.setText(f'Файлы не подключены, ничего не скопировано: {error}' if error
                                 else 'Таблица и шрифт проверены и скопированы.')
        self.refresh_play()

    def discover_game(self) -> None:
        if self.path_manager is not None:
            self.task('game-discovery', {'data_root': str(self.path_manager.root)}, self.show_game_discovery)

    def show_game_discovery(self, result) -> None:
        installations = result.get('installations', [])
        self.play_installation = installations[0] if installations else None
        self.refresh_play()

    # -- worker (same EditorJob contract as EditorWindow) --------------------------------------

    def task(self, action, arguments, callback=None):
        if self.job is not None:
            return
        self.last_error, self._result = '', None
        self._active_action, self._callback = action, callback
        self.job = EditorJob(self.workspace, action, arguments, self)
        self.job.result.connect(self.receive)
        self.job.failed.connect(self.failure)
        self.job.finished.connect(self.completed)
        self.job.start()

    @Slot(object)
    def receive(self, result):
        self._result = result

    @Slot(str)
    def failure(self, message):
        self.last_error = message

    @Slot()
    def completed(self):
        job, callback, result, action = self.job, self._callback, self._result, self._active_action
        self.job = None
        job.deleteLater()
        if not self.last_error and callback:
            callback(result)
        elif self.last_error and action == 'delta-update':
            self.delta_failed(self.last_error)
        elif self.last_error and action.startswith('native-'):
            self.play_failed(action, self.last_error)
        elif self.last_error:
            self.play_status.setText(self.last_error)
        if self.job is None and self._play_pending:
            self._play_pending = False
            self.refresh_play()
        if self.stack.currentIndex() == 1:
            self.update_settings()

    def cancel(self):
        if self.job is not None:
            self.job.cancelled.set()

    def closeEvent(self, event):
        if self.job is not None:
            self.job.cancelled.set()
            self.job.wait(5000)
        super().closeEvent(event)
