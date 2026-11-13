"""LauncherWindow part: the translation delta check at start and its status line on the Play page.

The check itself is `delta_updates.run_delta_update` in an `EditorJob` ('delta-update'), the only job at that moment, so the
project database is never open twice. It never builds or installs the package: «Играть» stays the player's button.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QTimer

from . import delta_updates as delta
from .play_page import ALERT, AMBER, DIM, GOLD, MUTED, SUCCESS, _label

STARTUP_DELAY_MS = 1500
RETRY_MS = 1500
TONES = {delta.UP_TO_DATE: SUCCESS, delta.APPLIED: GOLD, delta.WAITING_TABLE: AMBER, delta.TABLE_RECEIVED: GOLD,
         delta.NOTHING_PUBLISHED: DIM, delta.NETWORK_ERROR: MUTED, delta.HASH_ERROR: ALERT, delta.ERROR: ALERT}


class DeltaUpdateMixin:
    delta_status = None   # QLabel in the Play panel
    delta_state: str | None = None
    _delta_running = False

    def make_delta_status(self) -> None:
        self.delta_status = _label('', 13, MUTED)
        self.delta_status.setAccessibleName('Состояние обновлений перевода')
        layout = self.play_panel.layout()
        layout.insertWidget(layout.indexOf(self.play_note) + 1, self.delta_status)
        self.delta_status.hide()

    def schedule_delta_update(self) -> None:
        """Only the packaged app asks the network at start; a source checkout and the tests call `check_delta_update`."""
        if getattr(sys, 'frozen', False) and self.path_manager is not None:
            QTimer.singleShot(STARTUP_DELAY_MS, self.check_delta_update)

    def check_delta_update(self) -> None:
        if self.path_manager is None or self._delta_running:
            return
        if self.job is not None:  # a Play step is running: the database is theirs for now
            QTimer.singleShot(RETRY_MS, self.check_delta_update)
            return
        self._delta_running = True
        self.show_delta('Проверяю обновления перевода…', MUTED)
        self.task('delta-update', dict(data_root=str(self.path_manager.root)), self.delta_done)

    def delta_done(self, result: dict) -> None:
        self._delta_running = False
        self.delta_state = result['state']
        self.show_delta(result['message'], TONES.get(result['state'], MUTED))
        applied = result.get('applied') or {}
        if result['state'] in (delta.APPLIED, delta.WAITING_TABLE, delta.TABLE_RECEIVED) and (
                applied.get('applied') or result['state'] == delta.TABLE_RECEIVED):
            self.refresh_play()  # the project (or the table) changed: the package key is different now

    def delta_failed(self, message: str) -> None:
        self._delta_running = False
        self.delta_state = delta.ERROR
        self.show_delta(f'{delta.LABELS[delta.ERROR]}: {message}'[:300], ALERT)

    def show_delta(self, text: str, color: str) -> None:
        self.delta_status.setText(text)
        self.delta_status.setStyleSheet(f'color: {color}; background: transparent;')
        self.delta_status.setVisible(bool(text))
