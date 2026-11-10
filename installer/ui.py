"""Frameless wizard over Wuthering Waves key art: Welcome -> Folder -> Install -> Done
(also an uninstall flow). Layout: a frosted glass panel on the right of a full-bleed frame."""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QImage,QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QProgressBar, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from . import core

W, H = 900, 540
PANEL_RECT = (500, 20, 380, 500)          # glass panel over the right part of the art
TEXT, MUTED, DIM = '#f4ede0', '#c9bfae', '#a89d8b'
ACCENT, ACCENT_HOT, RED = '#e8b64a', '#f5d27e', '#d9443f'
FIELD, LINE = 'rgba(0,0,0,0.35)', 'rgba(255,236,200,0.20)'
BACKGROUND = 'background.jpg'             # embedded at build time; never stored in Git
FOCUS_X = 0.094                           # horizontal crop anchor of the default key art

QSS = f"""
* {{ font-family: 'Segoe UI'; color: {TEXT}; }}
#panel, #panel QWidget {{ background: transparent; }}
QLabel#eyebrow {{ color: {ACCENT}; font-size: 11px; font-weight: 600; letter-spacing: 2px; }}
QLabel#h1 {{ font-size: 26px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; font-size: 13px; }}
QLabel#dim {{ color: {DIM}; font-size: 12px; }}
QLabel#err {{ color: #ff9a8a; font-size: 12px; }}
QLabel#chip {{ border: 1px solid {LINE}; border-radius: 10px; padding: 3px 10px;
               color: {DIM}; font-size: 11px; }}
QLineEdit {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 10px;
             padding: 9px 12px; font-size: 13px; selection-background-color: {ACCENT};
             selection-color: #1a1208; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QPushButton {{ background: rgba(255,255,255,0.06); border: 1px solid {LINE}; border-radius: 10px;
               padding: 0 20px; min-height: 42px; font-size: 14px; }}
QPushButton:hover {{ border-color: {ACCENT}; background: rgba(255,255,255,0.10); }}
QPushButton:disabled {{ color: #6d6457; border-color: rgba(255,236,200,0.08); }}
QPushButton#primary {{ background: {ACCENT}; color: #1a1208; border: none; font-weight: 600;
                       padding: 0 26px; }}
QPushButton#primary:hover {{ background: {ACCENT_HOT}; }}
QPushButton#primary:disabled {{ background: rgba(232,182,74,0.25); color: #4a3f2f; }}
QPushButton#danger {{ background: {RED}; color: #fff4ea; border: none; font-weight: 600; padding: 0 26px; }}
QPushButton#danger:hover {{ background: #e5615c; }}
QPushButton#danger:disabled {{ background: rgba(217,68,63,0.25); color: #6d4a47; }}
QLabel#warn {{ color: #f2c27a; font-size: 12px; border: 1px solid rgba(242,163,94,0.35);
               border-radius: 10px; padding: 8px 10px; background: rgba(242,163,94,0.08); }}
QPushButton#x {{ background: transparent; border: none; padding: 0 8px; min-height: 28px;
                 color: {DIM}; font-size: 15px; }}
QPushButton#x:hover {{ color: #ff9a8a; background: transparent; }}
QCheckBox {{ font-size: 13px; spacing: 10px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {LINE};
                        background: {FIELD}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QProgressBar {{ background: rgba(255,255,255,0.08); border: none; border-radius: 4px;
                max-height: 8px; min-height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 4px;
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 {RED},stop:1 {ACCENT}); }}
"""


def cover(img: QImage, w: int, h: int, focus_x: float = FOCUS_X) -> QPixmap:
    """Scale to cover w x h, crop horizontally from focus_x, vertically centred."""
    s = max(w / img.width(), h / img.height())
    sw, sh = max(round(img.width() * s), w), max(round(img.height() * s), h)
    scaled = img.scaled(sw, sh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    x = min(sw - w, round(sw * focus_x))
    return QPixmap.fromImage(scaled.copy(x, (sh - h) // 2, w, h))


def blur(pm: QPixmap) -> QPixmap:
    """Cheap, strong blur: downscale twice, then scale back smoothly."""
    small = pm.scaled(max(pm.width() // 6, 1), max(pm.height() // 6, 1),
                      Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    tiny = small.scaled(max(small.width() // 4, 1), max(small.height() // 4, 1),
                        Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return tiny.scaled(pm.width(), pm.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def fallback_art(w: int, h: int) -> QPixmap:
    """Night sky with a moon, used when the build embedded no key art."""
    pm = QPixmap(w, h)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0, QColor('#2a2018'))
    g.setColorAt(1, QColor('#0d0b0a'))
    p.fillRect(0, 0, w, h, g)
    moon = QRadialGradient(QPointF(w * 0.42, h * 0.32), h * 0.36)
    moon.setColorAt(0, QColor(250, 226, 170, 235))
    moon.setColorAt(0.55, QColor(232, 182, 74, 90))
    moon.setColorAt(1, QColor(232, 182, 74, 0))
    p.fillRect(0, 0, w, h, moon)
    for i in range(7):
        p.setPen(QPen(QColor(232, 182, 74, 18 + i * 10), 1.4))
        line = QPainterPath()
        base = h * (0.62 + i * 0.045)
        for x in range(0, w + 10, 6):
            y = base + math.sin(x / 46 + i * 0.7) * (10 + i * 3)
            (line.moveTo if x == 0 else line.lineTo)(QPointF(x, y))
        p.drawPath(line)
    p.end()
    return pm


def load_art(w: int = W, h: int = H) -> tuple[QPixmap, bool]:
    """Embedded key art cropped to the window, or the painted fallback."""
    img = QImage(str(core.resource_path(BACKGROUND)))
    if img.isNull() or img.width() < 64 or img.height() < 64:
        return fallback_art(w, h), False
    return cover(img, w, h), True


class Backdrop(QWidget):
    """Whole window: key art, readability gradients, rounded clip and the title."""

    def __init__(self, art: QPixmap, parent=None):
        super().__init__(parent)
        self.art = art

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0, 0, w, h), 16, 16)
        p.setClipPath(clip)
        p.drawPixmap(0, 0, self.art)
        side = QLinearGradient(0, 0, w, 0)
        side.setColorAt(0.30, QColor(10, 8, 7, 0))
        side.setColorAt(0.62, QColor(10, 8, 7, 140))
        side.setColorAt(1.00, QColor(10, 8, 7, 217))
        p.fillRect(0, 0, w, h, side)
        foot = QLinearGradient(0, h - 150, 0, h)
        foot.setColorAt(0, QColor(10, 8, 7, 0))
        foot.setColorAt(1, QColor(10, 8, 7, 205))
        p.fillRect(0, h - 150, w, 150, foot)
        p.setFont(QFont('Segoe UI', 22, QFont.DemiBold))
        p.setPen(QColor(TEXT))
        p.drawText(32, h - 58, 'WuWa ')
        x = 32 + p.fontMetrics().horizontalAdvance('WuWa ')
        p.setPen(QColor(ACCENT))
        p.drawText(x, h - 58, 'Russian')
        p.setFont(QFont('Segoe UI', 10))
        p.setPen(QColor('#d9cfbf'))
        p.drawText(33, h - 32, 'Перевод Wuthering Waves · English → Русский')
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 236, 200, 30), 1))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 16, 16)


class Glass(QFrame):
    """Frosted panel: the blurred slice of the art under it, tinted dark."""

    def __init__(self, blurred: QPixmap, parent=None):
        super().__init__(parent)
        self.setObjectName('panel')
        self.blurred = blurred

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.setClipPath(path)
        p.drawPixmap(0, 0, self.blurred, self.x(), self.y(), self.width(), self.height())
        p.fillRect(r, QColor(14, 11, 10, 184))
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 236, 200, 36), 1))
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)


class Worker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, payload: Path, target: Path, desktop: bool, menu: bool, wait_pid: int = 0):
        super().__init__()
        self.payload, self.target, self.desktop, self.menu = payload, target, desktop, menu
        self.wait_pid = wait_pid
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if self.wait_pid:
                self.progress.emit(0, 1, 'Жду, пока лаунчер закроется…')
                if not core.wait_for_exit(self.wait_pid, WAIT_LAUNCHER_SECONDS):
                    raise core.InstallError('Лаунчер не закрылся. Закрой его и запусти обновление снова.')
            uninstaller = core.resource_path(core.UNINSTALLER)
            if not uninstaller.is_file():  # older builds: a copy of this installer
                uninstaller = Path(sys.executable) if getattr(sys, 'frozen', False) else None
            m = core.install(self.payload, self.target, self.progress.emit,
                             lambda: self._cancel, uninstaller)
            warn = ''
            links = []
            try:
                if self.desktop or self.menu:  # an update keeps the shortcuts it already has
                    links = core.create_shortcuts(self.target, self.desktop, self.menu)
            except Exception:
                warn = 'Ярлыки создать не удалось — запускай приложение из папки установки.'
            try:
                core.refresh_shell_icons([self.target / core.APP_EXE, self.target / core.UNINSTALLER,
                                          *map(Path, links)])
            except Exception:  # noqa: BLE001 - only a cosmetic refresh
                pass
            if (self.target / core.UNINSTALLER).is_file():
                core.register_uninstall(self.target, m.get('version'))
            m['warning'] = warn
            self.finished_ok.emit(m)
        except core.InstallError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - show any failure to the user
            self.failed.emit(f'Непредвиденная ошибка: {exc}')


class UninstallWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(self, target: Path, remove_data: bool):
        super().__init__()
        self.target, self.remove_data = target, remove_data

    def run(self):
        try:
            self.finished_ok.emit(core.uninstall(self.target, self.progress.emit, self.remove_data))
        except core.InstallError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - show any failure to the user
            self.failed.emit(f'Непредвиденная ошибка: {exc}')


PROGRESS_PAGE, DONE_PAGE = 2, 3
UNINSTALL_PAGE, UNINSTALL_PROGRESS, UNINSTALL_DONE = 4, 5, 6
WAIT_LAUNCHER_SECONDS = 60


def window_icon(uninstall: bool) -> QIcon:
    path = core.resource_path('uninstall.ico' if uninstall else 'setup.ico')
    return QIcon(str(path)) if path.is_file() else QIcon()


class Wizard(QWidget):
    def __init__(self, payload: Path, uninstall_dir: Path | None = None, update_dir: Path | None = None,
                 wait_pid: int = 0):
        super().__init__()
        self.payload, self.worker, self._drag = payload, None, None
        self.uninstall_dir, self.update_dir, self.wait_pid = uninstall_dir, update_dir, wait_pid
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        action = 'Удаление' if uninstall_dir else 'Обновление' if update_dir else 'Установка'
        self.setWindowTitle(f'{action} — {core.APP_NAME}')
        self.setWindowIcon(window_icon(bool(uninstall_dir)))
        self.setFixedSize(W, H)
        self.setStyleSheet(QSS)
        info = core.payload_info(payload) if payload.is_file() else {}
        self.version = info.get('version', '?')
        self.has_translation = bool(info.get('translation'))

        art, self.has_art = load_art()
        root = Backdrop(art, self)
        root.setObjectName('root')
        root.setGeometry(0, 0, W, H)
        panel = Glass(blur(art), root)
        panel.setGeometry(*PANEL_RECT)
        rl = QVBoxLayout(panel)
        rl.setContentsMargins(28, 8, 10, 22)
        rl.setSpacing(0)
        top = QHBoxLayout()
        top.addStretch()
        close = QPushButton('✕')
        close.setObjectName('x')
        close.setCursor(Qt.PointingHandCursor)
        close.setToolTip('Закрыть')
        close.clicked.connect(self.close_request)
        top.addWidget(close)
        rl.addLayout(top)
        self.stack = QStackedWidget()
        rl.addWidget(self.stack)

        self.stack.addWidget(self._welcome())
        self.stack.addWidget(self._folder())
        self.stack.addWidget(self._progress())
        self.stack.addWidget(self._done())
        self.stack.addWidget(self._uninstall())
        self.stack.addWidget(self._uninstall_progress())
        self.stack.addWidget(self._uninstall_done())
        if uninstall_dir:
            self.stack.setCurrentIndex(UNINSTALL_PAGE)
        elif update_dir:
            self.progress_step.setText('ОБНОВЛЕНИЕ')
            self.progress_title.setText(f'Обновление до {self.version}…')
            self.progress_text.setText('Ставлю новую версию поверх старой. Перевод, правки и настройки '
                                       'остаются. Лаунчер откроется сам.')
            self.cancel_btn.hide()
            self.stack.setCurrentIndex(PROGRESS_PAGE)
            QTimer.singleShot(0, self.start_update)

    # -- pages
    def _page(self, title: str, sub: str, eyebrow: str = ''):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 18, 0)
        v.setSpacing(10)
        e = QLabel(eyebrow.upper())
        e.setObjectName('eyebrow')
        v.addWidget(e)
        t = QLabel(title)
        t.setObjectName('h1')
        t.setWordWrap(True)
        s = QLabel(sub)
        s.setObjectName('muted')
        s.setWordWrap(True)
        v.addWidget(t)
        v.addWidget(s)
        return w, v

    def _buttons(self, v, *btns):
        v.addStretch()
        r = QHBoxLayout()
        r.addStretch()
        for b in btns:
            r.addWidget(b)
        v.addLayout(r)

    def _btn(self, text, slot, primary=False):
        b = QPushButton(text)
        if primary:
            b.setObjectName('primary')
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(slot)
        return b

    def _translation_line(self) -> str:
        if self.has_translation:
            return 'Готовый перевод внутри: ставится сразу'
        return 'Перевод не вложен: его нужно будет подключить'

    def _welcome(self):
        w, v = self._page('Добро пожаловать',
                          'Мастер установит лаунчер WuWa Russian с готовым русским переводом '
                          'имён и интерфейса. После установки — одна кнопка «Играть».',
                          'Шаг 1 из 3')
        v.addSpacing(4)
        for line in (self._translation_line(),
                     'Установка без прав администратора, игру не трогает',
                     'Твои правки перевода при обновлении сохраняются'):
            c = QLabel(f'<span style="color:{ACCENT}">●</span>&nbsp;&nbsp;{line}')
            c.setStyleSheet('font-size:13px;')
            v.addWidget(c)
        chip = QLabel(f'Версия {self.version}')
        chip.setObjectName('chip')
        chip.setAlignment(Qt.AlignLeft)
        v.addSpacing(6)
        r = QHBoxLayout()
        r.addWidget(chip)
        r.addStretch()
        v.addLayout(r)
        self._buttons(v, self._btn('Далее', lambda: self.stack.setCurrentIndex(1), True))
        return w

    def _folder(self):
        w, v = self._page('Куда установить', 'Если папка не пуста, программа ляжет '
                          'во вложенную папку WuWaRussian.', 'Шаг 2 из 3')
        self.path = QLineEdit(str(core.default_target()))
        pick = self._btn('Обзор…', self.browse)
        r = QHBoxLayout()
        r.addWidget(self.path)
        r.addWidget(pick)
        v.addSpacing(6)
        v.addLayout(r)
        self.err = QLabel('')
        self.err.setObjectName('err')
        self.err.setWordWrap(True)
        v.addWidget(self.err)
        self.cb_desktop = QCheckBox('Ярлык на рабочем столе')
        self.cb_desktop.setChecked(True)
        self.cb_menu = QCheckBox('Ярлык в меню «Пуск»')
        self.cb_menu.setChecked(True)
        v.addWidget(self.cb_desktop)
        v.addWidget(self.cb_menu)
        try:
            need = core.payload_size(self.payload) // 2**20 + 1
            n = QLabel(f'Потребуется около {need} МБ свободного места.')
            n.setObjectName('dim')
            v.addWidget(n)
        except Exception:  # noqa: BLE001
            pass
        self._buttons(v, self._btn('Назад', lambda: self.stack.setCurrentIndex(0)),
                      self._btn('Установить', self.start_install, True))
        return w

    def _progress(self):
        w, v = self._page('Установка…', 'Копирую программу и перевод, это займёт около минуты.',
                          'Шаг 3 из 3')
        self.progress_step, self.progress_title, self.progress_text = w.findChildren(QLabel)[:3]
        v.addSpacing(14)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        v.addWidget(self.bar)
        self.cur = QLabel('')
        self.cur.setObjectName('dim')
        v.addWidget(self.cur)
        self.cancel_btn = self._btn('Отмена', self.cancel_install)
        self._buttons(v, self.cancel_btn)
        return w

    def _done(self):
        w, v = self._page('Готово', '', 'Установка завершена')
        self.done_title = w.findChildren(QLabel)[1]
        self.done_text = w.findChildren(QLabel)[2]
        self.cb_launch = QCheckBox('Открыть лаунчер')
        self.cb_launch.setChecked(True)
        v.addSpacing(10)
        v.addWidget(self.cb_launch)
        self.finish_btn = self._btn('Завершить', self.finish, True)
        self._buttons(v, self.finish_btn)
        return w

    def _uninstall(self):
        w, v = self._page('Удалить WuWa Russian?', 'Мастер уберёт программу с этого компьютера.',
                          'Шаг 1 из 2 · удаление')
        v.addSpacing(4)
        for line in ('Файлы программы, ярлыки и запись в «Приложениях»',
                     'Без прав администратора, игру не трогает',
                     'Перевод и настройки остаются, если не отметить ниже'):
            c = QLabel(f'<span style="color:{RED}">●</span>&nbsp;&nbsp;{line}')
            c.setStyleSheet('font-size:13px;')
            c.setWordWrap(True)
            v.addWidget(c)
        v.addSpacing(6)
        self.cb_data = QCheckBox('Удалить и мои данные: перевод, правки,\nнастройки, резервные копии')
        v.addWidget(self.cb_data)
        in_game = bool(self.uninstall_dir) and core.translation_in_game(self.uninstall_dir)
        self.un_warn = QLabel('Перевод сейчас подключён к игре. Чтобы убрать его из папки игры, '
                              'сначала открой лаунчер → «Настройки» → «Убрать перевод из игры».')
        self.un_warn.setObjectName('warn')
        self.un_warn.setWordWrap(True)
        self.un_warn.setVisible(in_game)
        v.addWidget(self.un_warn)
        self.un_msg = QLabel('')
        self.un_msg.setObjectName('err')
        self.un_msg.setWordWrap(True)
        v.addWidget(self.un_msg)
        self.un_btn = self._btn('Удалить', self.do_uninstall)
        self.un_btn.setObjectName('danger')
        buttons = [self._btn('Отмена', self.close), self.un_btn]
        if in_game:
            buttons.insert(0, self._btn('Лаунчер', self.open_launcher))
        self._buttons(v, *buttons)
        return w

    def _uninstall_progress(self):
        w, v = self._page('Удаление…', 'Убираю файлы программы и ярлыки.', 'Шаг 2 из 2 · удаление')
        v.addSpacing(14)
        self.un_bar = QProgressBar()
        self.un_bar.setRange(0, 1000)
        v.addWidget(self.un_bar)
        self.un_cur = QLabel('')
        self.un_cur.setObjectName('dim')
        v.addWidget(self.un_cur)
        v.addStretch()
        return w

    def _uninstall_done(self):
        w, v = self._page('Готово', '', 'Удаление завершено')
        self.un_done_text = w.findChildren(QLabel)[2]
        self._buttons(v, self._btn('Завершить', self.close, True))
        return w

    # -- actions
    def browse(self):
        d = QFileDialog.getExistingDirectory(self, 'Папка установки', self.path.text())
        if d:
            self.path.setText(str(Path(d)))

    def start_install(self):
        try:
            self.target = core.resolve_target(self.path.text())
            core.check_writable(self.target)
        except core.InstallError as exc:
            self.err.setText(str(exc))
            return
        self.err.setText('')
        self.stack.setCurrentIndex(2)
        self.worker = Worker(self.payload, self.target, self.cb_desktop.isChecked(),
                             self.cb_menu.isChecked())
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_ok)
        self.worker.failed.connect(self.on_fail)
        self.worker.start()

    def start_update(self):
        self.target = self.update_dir
        self.worker = Worker(self.payload, self.target, False, False, self.wait_pid)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_ok)
        self.worker.failed.connect(self.on_fail)
        self.worker.start()

    def on_progress(self, done, total, name):
        self.bar.setValue(int(done * 1000 / max(total, 1)))
        self.cur.setText(name if len(name) < 60 else '…' + name[-58:])

    def cancel_install(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)

    def on_ok(self, manifest):
        if self.update_dir:
            self.launch(self.target)
            self.close()
            return
        self.done_title.setText('Готово')
        msg = f'Установлено в\n{self.target}'
        if manifest.get('warning'):
            msg += '\n\n' + manifest['warning']
        self.done_text.setText(msg)
        self.stack.setCurrentIndex(3)

    def on_fail(self, text):
        if self.update_dir:
            self.done_title.setText('Обновить не удалось')
            self.done_text.setText(f'{text}\n\nПеревод и настройки не тронуты. Открой лаунчер и попробуй '
                                   'ещё раз или скачай установщик со страницы релизов.')
            self.stack.setCurrentIndex(DONE_PAGE)
            return
        self.stack.setCurrentIndex(1)
        self.err.setText(text)

    @staticmethod
    def launch(folder: Path):
        exe = folder / core.APP_EXE
        if exe.is_file():
            subprocess.Popen([str(exe)], cwd=str(folder), close_fds=True)

    def finish(self):
        if self.cb_launch.isChecked() and self.stack.currentIndex() == DONE_PAGE:
            self.launch(self.target)
        self.close()

    def open_launcher(self):
        self.launch(self.uninstall_dir)
        self.close()

    def do_uninstall(self):
        self.un_msg.setText('')
        self.stack.setCurrentIndex(UNINSTALL_PROGRESS)
        self.worker = UninstallWorker(self.uninstall_dir, self.cb_data.isChecked())
        self.worker.progress.connect(self.on_uninstall_progress)
        self.worker.finished_ok.connect(self.on_uninstalled)
        self.worker.failed.connect(self.on_uninstall_failed)
        self.worker.start()

    def on_uninstall_progress(self, done, total, name):
        self.un_bar.setValue(int(done * 1000 / max(total, 1)))
        self.un_cur.setText(name if len(name) < 60 else '…' + name[-58:])

    def on_uninstalled(self, count):
        kept = '' if self.cb_data.isChecked() else '\n\nПеревод и настройки остались в папке Data.'
        self.un_done_text.setText(f'WuWa Russian удалён, файлов: {count}.{kept}')
        self.stack.setCurrentIndex(UNINSTALL_DONE)
        self.schedule_self_delete()

    def on_uninstall_failed(self, text):
        self.stack.setCurrentIndex(UNINSTALL_PAGE)
        self.un_msg.setText(text)

    def schedule_self_delete(self):
        """A running EXE cannot delete itself: a hidden cmd removes it a few seconds after exit."""
        if not getattr(sys, 'frozen', False):
            return
        exe = Path(sys.executable)
        if exe.parent != self.uninstall_dir:
            return
        subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & del /f /q "{exe}" & '
                         f'rmdir "{exe.parent}"', shell=True, creationflags=0x08000000)

    def close_request(self):
        if (isinstance(self.worker, UninstallWorker) or self.update_dir) and self.worker \
                and self.worker.isRunning():
            return  # removal and updates run to the end; stopping halfway leaves a mixed folder
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(self, 'Отмена', 'Прервать установку?') != QMessageBox.Yes:
                return
            self.worker.cancel()
            self.worker.wait(5000)
        self.close()

    # frameless drag
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 60:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _):
        self._drag = None


def _arg(argv: list[str], name: str) -> str:
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return ''


def run(argv: list[str]) -> int:
    app = QApplication(argv)
    payload = core.resource_path('payload.zip')
    un = None
    frozen = getattr(sys, 'frozen', False)
    exe = Path(sys.executable)
    if '--uninstall' in argv or (frozen and core.is_uninstaller(exe)):
        un = core.uninstall_target(exe, frozen)
        if un is None:
            QMessageBox.information(None, core.APP_NAME, 'Установка не найдена.')
            return 0
    elif not payload.is_file():
        QMessageBox.critical(None, core.APP_NAME, 'Пакет установки (payload.zip) не найден.')
        return 1
    update, pid = None, 0
    if un is None and '--update' in argv:
        try:
            update = core.update_target(_arg(argv, '--update'))
            pid = int(_arg(argv, '--wait-pid') or 0)
        except (core.InstallError, ValueError) as exc:
            QMessageBox.critical(None, core.APP_NAME, str(exc) or 'Неверные параметры обновления.')
            return 1
    w = Wizard(payload, un, update, pid)
    w.show()
    return app.exec()
