"""Launcher side of self-update: background check, «Обновить?» dialog, verified download, hand-over.

Network work runs on daemon threads (a hung connection never blocks closing the window) and reports
back through Qt signals. All checks and file handling live in `updater`; this is only the UI.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QProgressBar, QVBoxLayout

from . import __version__, updater
from .play_page import GOLD, GROUND, HEADING, SOFT, TEXT, CutButton, CutPanel, Diamond, _label, rgba

STARTUP_DELAY_MS = 2500


class _Bridge(QObject):
    found = Signal(object)          # Release, or None when nothing newer
    failed = Signal(str)
    progress = Signal(int, int)
    downloaded = Signal(str)


def _spawn(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


class UpdateDialog(QDialog):
    """Shows the new version; on «Обновить» downloads, verifies and starts the installer."""

    def __init__(self, release: updater.Release, folder: Path, target: Path, parent=None):
        super().__init__(parent)
        self.release, self.folder, self.target = release, Path(folder), Path(target)
        self._cancel = threading.Event()
        self._busy = False
        self.bridge = _Bridge(self)
        self.bridge.progress.connect(self.on_progress)
        self.bridge.downloaded.connect(self.on_downloaded)
        self.bridge.failed.connect(self.on_failed)
        self.setWindowTitle('Обновление WuWa Russian')
        self.setStyleSheet(f'QDialog {{ background: {GROUND}; }} QProgressBar {{ background: rgba(255,255,255,20);'
                           f' border: 0; max-height: 6px; min-height: 6px; color: transparent; }}'
                           f' QProgressBar::chunk {{ background: {GOLD}; }}')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        panel = CutPanel(cut=20)
        panel.tone, panel.fill = GOLD, rgba('#0E0F13', 0.94)
        panel.setMinimumWidth(520)
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(16)
        heading = QHBoxLayout()
        mark = Diamond(10)
        mark.tone = GOLD
        heading.addWidget(mark)
        heading.addWidget(_label(f'Вышла версия {release.version}', 22, TEXT, HEADING, QFont.DemiBold, wrap=False))
        heading.addStretch(1)
        layout.addLayout(heading)
        layout.addWidget(_label(f'У тебя {__version__}. Обновление скачается, проверится по подписи и '
                                'встанет поверх. Перевод, правки и настройки остаются.', 15, SOFT))
        notes = release.notes.strip()
        if notes:
            layout.addWidget(_label(notes if len(notes) <= 600 else notes[:600].rstrip() + '…', 13, '#B5AEA1'))
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        layout.addWidget(self.bar)
        self.status = _label('', 13, '#B5AEA1')
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.later_button = CutButton('Позже')
        self.later_button.clicked.connect(self.reject)
        self.update_button = CutButton('Обновить')
        self.update_button.clicked.connect(self.start_download)
        actions.addWidget(self.later_button)
        actions.addWidget(self.update_button)
        layout.addLayout(actions)
        self.update_button.setDefault(True)

    def start_download(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._cancel.clear()
        self.update_button.setEnabled(False)
        self.later_button.setText('Отмена')
        self.bar.setValue(0)
        self.bar.show()
        self.status.setText('Скачиваю…')
        _spawn(self._download)

    def _download(self) -> None:
        try:
            path = updater.download(self.release, self.folder, progress=self.bridge.progress.emit,
                                    cancelled=self._cancel.is_set)
            self.bridge.downloaded.emit(str(path))
        except updater.Cancelled:
            pass
        except updater.UpdateError as exc:
            self.bridge.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - disk full, permissions: shown, never crash the launcher
            self.bridge.failed.emit(f'Не удалось скачать обновление: {exc}')

    def on_progress(self, done: int, total: int) -> None:
        self.bar.setValue(int(done * 1000 / max(total, 1)))
        self.status.setText(f'Скачиваю… {done // 2**20} из {total // 2**20 + 1} МБ')

    def on_downloaded(self, path: str) -> None:
        self.status.setText('Проверено. Запускаю установку, лаунчер закроется и откроется снова…')
        try:
            updater.start_installer(Path(path), self.target)
        except OSError as exc:
            self.on_failed(f'Не удалось запустить установщик: {exc}')
            return
        self._busy = False
        self.accept()

    def on_failed(self, message: str) -> None:
        self._busy = False
        self.bar.hide()
        self.status.setText(message)
        self.update_button.setText('Повторить')
        self.update_button.setEnabled(True)
        self.later_button.setText('Позже')

    def reject(self) -> None:
        self._cancel.set()
        self._busy = False
        super().reject()


class UpdateMixin:
    """LauncherWindow part: quiet check after start, «Проверить обновления» in settings."""

    update_status = None   # QLabel in the settings footer
    _update_checking = False

    def schedule_update_check(self) -> None:
        if updater.install_root() is not None:
            QTimer.singleShot(STARTUP_DELAY_MS, lambda: self.check_updates(quiet=True))

    def check_updates(self, quiet: bool = False) -> None:
        if self._update_checking:
            return
        self._update_checking = True
        bridge = _Bridge(self)  # one per check, so each answer knows whether its check was quiet
        bridge.found.connect(lambda release: self._update_found(release, quiet))
        bridge.failed.connect(lambda message: self._update_failed(message, quiet))
        self._set_update_status('Проверяю обновления…' if not quiet else '')
        _spawn(self._check, bridge)

    @staticmethod
    def _check(bridge: _Bridge) -> None:
        try:
            release = updater.latest_release()
            bridge.found.emit(release if updater.is_newer(release) else None)
        except updater.UpdateError as exc:
            bridge.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            bridge.failed.emit(f'Не удалось проверить обновления: {exc}')

    def _set_update_status(self, text: str) -> None:
        if self.update_status is not None:
            self.update_status.setText(text)

    def _update_failed(self, message: str, quiet: bool) -> None:
        self._update_checking = False
        self._set_update_status('' if quiet else message)

    def _update_found(self, release, quiet: bool) -> None:
        self._update_checking = False
        if release is None:
            self._set_update_status('' if quiet else f'Установлена последняя версия ({__version__}).')
            return
        root = updater.install_root()
        if root is None:
            self._set_update_status(f'Вышла версия {release.version}. Сама обновляется только установленная '
                                    'программа — скачай установщик со страницы релизов.')
            return
        if self.job is not None:  # a Play/remove step is running: never pull the folder from under it
            self._set_update_status(f'Вышла версия {release.version}. Обновлю, когда закончится текущее действие.')
            QTimer.singleShot(5000, lambda: self._update_found(release, quiet))
            self._update_checking = True
            return
        self._set_update_status(f'Вышла версия {release.version}.')
        folder = self.path_manager.data / 'Updates' if self.path_manager is not None else root / 'Data' / 'Updates'
        if self.open_update_dialog(release, folder, root) == QDialog.Accepted:
            self.close()
            QApplication.instance().quit()

    def open_update_dialog(self, release, folder: Path, root: Path) -> int:
        return UpdateDialog(release, folder, root, self).exec()
