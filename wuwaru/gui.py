"""Functional M3 desktop editor. Start: python -m wuwaru.gui --workspace PATH."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Slot, QSettings
from PySide6.QtGui import QAction, QKeySequence, QFontDatabase, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTableView, QTableWidget, QTableWidgetItem, QTabBar, QToolButton, QVBoxLayout, QWidget,
    QListWidget, QStackedWidget)

from .editor import CATEGORIES
from .editor_worker import EditorJob
from .native_helper import helper_main
from .desktop_pages import ProductPageMixin
from .native_launch import RESOURCE
from .play_page import PlayPageMixin
from .provider_page import ProviderPageMixin
from .portable import PathManager, WorkspaceDiscoveryService
from .translation_db import STATUSES
from .validation import TranslationValidator


class RecordModel(QAbstractTableModel):
    columns = (('id_value', 'ID'), ('source', 'English'), ('target', 'Русский'),
               ('status', 'Статус'), ('category', 'Категория'), ('resource', 'Источник'),
               ('merge_status', 'Импорт / конфликты'))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and role in (Qt.DisplayRole, Qt.ToolTipRole):
            row = self.rows[index.row()]
            if role == Qt.ToolTipRole and index.column() == 0:
                return f"{row['id_type']} ID · {row['table_name']} · seq {row['seq']}"
            value = row.get(self.columns[index.column()][0])
            return str(value or '')[:500]
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[section][1]
        return None

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class EditorWindow(PlayPageMixin, ProviderPageMixin, ProductPageMixin, QMainWindow):
    def __init__(self, workspace: Path, path_manager: PathManager | None = None, known_roots=()):
        super().__init__()
        # Windows offscreen/sandbox platforms may not enumerate system fonts.
        # Read the installed font; never download or bundle a replacement.
        if sys.platform == 'win32' and not QFontDatabase.families():
            QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/segoeui.ttf'))
            QApplication.instance().setFont(QFont('Segoe UI', 10))
        self.workspace = Path(workspace)
        self.path_manager = path_manager
        self.known_roots = tuple(known_roots)
        self.job = None
        self.current = None
        self.pages = [0]
        self.last_error = ''
        self.closing = False
        self.setWindowTitle('WuWa Russian')
        self.resize(1440, 920)
        self.body = QWidget()
        layout = QVBoxLayout(self.body)
        self.search = QLineEdit()
        self.search.setPlaceholderText('ID, английский или русский текст — Enter для поиска')
        self.search.setAccessibleName('Поиск по ID и тексту')
        self.search.returnPressed.connect(self.reload)
        self.category = QComboBox()
        self.category.addItems(['Все категории', *CATEGORIES])
        self.status = QComboBox()
        self.status.addItems(['Все статусы', *STATUSES])
        self.resource = QLineEdit()
        self.resource.setPlaceholderText('Источник: точное имя .db или .json')
        self.resource.setAccessibleName('Фильтр по источнику')
        self.severity = QComboBox()
        self.severity.addItems(['Все уровни QA', 'ERROR', 'WARNING', 'INFO'])
        filters = QHBoxLayout()
        for widget in (self.search, self.category, self.status, self.resource, self.severity):
            filters.addWidget(widget)
        self.button(filters, 'Найти', self.reload)
        layout.addLayout(filters)
        self.source_type = QComboBox()
        self.source_type.addItems(['Все типы источников', 'SQLITE_STAGING',
                                   'REAL_LOCAL_RESOURCE', 'PUBLIC_TEXTMAP', 'SYNTHETIC_FIXTURE'])
        self.source_type.setAccessibleName('Тип источника данных')
        layout.addWidget(self.source_type)
        self.merge_filter = QComboBox()
        self.merge_filter.addItems(['Все строки импорта', 'Конфликты переводов', 'Есть кандидаты'])
        self.merge_filter.setAccessibleName('Фильтр конфликтов импорта')
        layout.addWidget(self.merge_filter)
        self.model = RecordModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setAccessibleName('Исходные строки и переводы')
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setColumnWidth(1, 320)
        self.table.setColumnWidth(2, 320)
        self.table.setColumnWidth(6, 180)
        self.table.selectionModel().currentRowChanged.connect(self.select_record)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        pane = QWidget()
        pane_layout = QVBoxLayout(pane)
        self.identity = QLabel('Выбери строку в таблице')
        self.identity.setTextFormat(Qt.PlainText)
        self.identity.setWordWrap(True)
        pane_layout.addWidget(self.identity)
        self.provenance = QPlainTextEdit()
        self.provenance.setReadOnly(True)
        self.provenance.setMaximumHeight(95)
        self.provenance.setAccessibleName('Происхождение и версия источника')
        pane_layout.addWidget(self.provenance)
        texts = QHBoxLayout()
        self.source, self.target = QPlainTextEdit(), QPlainTextEdit()
        self.source.setReadOnly(True)
        self.source.setAccessibleName('Английский оригинал')
        self.target.setAccessibleName('Русский перевод')
        for label, widget in (('English — оригинал', self.source), ('Русский — перевод', self.target)):
            column = QVBoxLayout()
            column.addWidget(QLabel(label))
            column.addWidget(widget)
            texts.addLayout(column)
        pane_layout.addLayout(texts)
        self.context = QLineEdit()
        form = QFormLayout()
        form.addRow('Контекст:', self.context)
        pane_layout.addLayout(form)
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMaximumHeight(90)
        self.diagnostics.setAccessibleName('Результаты QA выбранной строки')
        pane_layout.addWidget(self.diagnostics)
        actions = QHBoxLayout()
        for index, (text, callback) in enumerate((('Проверить', self.validate_current), ('Сохранить · Ctrl+S', self.save),
                               ('Утвердить · Ctrl+Enter', lambda: self.save(True)),
                               ('История / откат', self.show_history), ('Категория…', self.change_category),
                               ('Контекст сохранить', self.save_context), ('Связать контекст…', self.link_context))):
            if index == 4:
                pane_layout.addLayout(actions)
                actions = QHBoxLayout()
            self.button(actions, text, callback)
        pane_layout.addLayout(actions)
        splitter.addWidget(pane)
        splitter.setSizes([400, 400])
        layout.addWidget(splitter)
        navigation = QHBoxLayout()
        self.button(navigation, '← Назад', self.previous_page)
        self.button(navigation, 'Далее →', self.next_page)
        self.page_label = QLabel()
        navigation.addWidget(self.page_label)
        self.button(navigation, 'Статус выбранных…', self.batch_status)
        layout.addLayout(navigation)
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 6)
        outer.setSpacing(0)
        # Stack indices 0..9 are unchanged; the Play page is appended at the end.
        self.navigation_labels = ['Главная', 'Игровые ресурсы', 'База переводов',
                                  'Автоматический перевод', 'Редактор',
                                  'Проверка качества', 'Экспорт', 'Настройки',
                                  'Нативная совместимость', 'Файлы проекта', 'Играть']
        self.stack = QStackedWidget()
        self.page_widgets = [self.make_home_page(), self.make_resources_page(),
                             self.make_database_page(), self.make_translation_page(),
                             self.body, self.make_qa_page(), self.make_export_page(),
                             self.make_settings_page(), self.make_compatibility_page(),
                             self.make_files_page(), self.make_play_page()]
        for page in self.page_widgets:
            self.stack.addWidget(page)
        play_index = self.page_widgets.index(self.play_page)
        self.tab_pages = [('Играть', play_index), ('Перевод', 3), ('Редактор', 4), ('Проверка', 5),
                          ('Настройки', 7)]
        more_pages = [('Игровые ресурсы', 1), ('База переводов', 2), ('Экспорт', 6),
                      ('Нативная совместимость', 8), ('Файлы проекта', 9), ('Главная', 0)]
        header = QWidget()
        header.setObjectName('header')
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(32, 0, 32, 0)
        header_layout.setSpacing(18)
        brand = QLabel('WuWa Russian')
        brand.setObjectName('brand')
        brand.setToolTip(str(self.workspace))
        header_layout.addWidget(brand)
        self.tabs = QTabBar()
        self.tabs.setObjectName('tabs')
        self.tabs.setAccessibleName('Разделы приложения')
        self.tabs.setDrawBase(False)
        self.tabs.setExpanding(False)
        for label, _ in self.tab_pages:
            self.tabs.addTab(label)
        # Hidden last tab: current while a page from «Ещё» is shown, so no visible tab is highlighted.
        self.tabs.addTab('')
        self.tabs.setTabVisible(len(self.tab_pages), False)
        self.tabs.currentChanged.connect(
            lambda position: self.navigate(self.tab_pages[position][1]) if position < len(self.tab_pages) else None)
        header_layout.addWidget(self.tabs)
        self.more_button = QToolButton()
        self.more_button.setObjectName('moreButton')
        self.more_button.setText('Ещё')
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        more_menu = QMenu(self.more_button)
        for label, index in more_pages:
            more_menu.addAction(label).triggered.connect(lambda checked=False, index=index: self.navigate(index))
        self.more_button.setMenu(more_menu)
        header_layout.addWidget(self.more_button)
        header_layout.addStretch(1)
        badge = QLabel('РЕСУРСЫ ' + RESOURCE.rsplit('/', 1)[-1])
        badge.setObjectName('resourceBadge')
        header_layout.addWidget(badge, 0, Qt.AlignVCenter)
        outer.addWidget(header)
        outer.addWidget(self.stack, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 6, 12, 0)
        self.message = QLabel('Готово')
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        footer.addWidget(self.message, 1)
        cancel = QPushButton('Остановить')
        cancel.clicked.connect(self.cancel)
        footer.addWidget(cancel)
        outer.addLayout(footer)
        self.setCentralWidget(container)
        self.setStyleSheet('''
            QMainWindow, QWidget { background: #0B0D10; color: #F5EFE2; }
            #header { background: #08090C; border-bottom: 1px solid rgba(233,185,73,64); min-height: 64px; }
            #header QLabel, #header QTabBar, #header QToolButton { background: transparent; }
            #brand { color: #F5EFE2; font-family: Unbounded, "Segoe UI"; font-size: 15px; font-weight: 600; padding-right: 10px; }
            QTabBar { background: transparent; }
            QTabBar::tab { background: transparent; color: #C8C1B2; border: 0; border-bottom: 2px solid transparent; padding: 12px 14px; min-height: 20px; }
            QTabBar::tab:selected { color: #F5E6C4; font-weight: 600; border-bottom: 2px solid #E9B949; }
            QTabBar::tab:hover { color: #F5E6C4; }
            #moreButton { background: transparent; color: #C8C1B2; border: 0; padding: 12px 14px; }
            #moreButton:hover { color: #F5E6C4; }
            #moreButton::menu-indicator { image: none; }
            #resourceBadge { color: #E9B949; border: 1px solid rgba(233,185,73,128); padding: 4px 10px; font-size: 12px; }
            QMenu { background: #12151A; border: 1px solid rgba(233,185,73,90); }
            QMenu::item { padding: 8px 20px; color: #E7DFCF; }
            QMenu::item:selected { background: #262A33; color: #F5E6C4; }
            QPushButton { background: #171A20; border: 1px solid #3A3426; border-radius: 3px; padding: 7px 11px; }
            QPushButton:hover { background: #22262E; border-color: #E9B949; }
            QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTableView, QTableWidget { background: #12151A; border: 1px solid #2C2F36; border-radius: 3px; selection-background-color: #5A4A1E; }
            QHeaderView::section { background: #171A20; padding: 5px; border: 0; }
            #playPage QWidget { background: transparent; }
        ''')
        self.navigate(play_index)
        for sequence, callback in (('Ctrl+S', self.save), ('Ctrl+Return', lambda: self.save(True)),
                                   ('Ctrl+F', self.search.setFocus), ('F5', self.reload), ('Escape', self.cancel)):
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(callback)
            self.addAction(action)
        self.refresh_summary_sync()
        if self.path_manager is not None:
            self.task('game-discovery', {'data_root': str(self.path_manager.root)},
                      self.show_game_discovery)
        else:
            self.reload()
        self.refresh_play()

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(lambda checked=False: callback())
        layout.addWidget(button)
        return button

    def task(self, action, arguments, callback=None):
        if self.job is not None:
            return
        self.last_error, self._result = '', None
        self._active_action = action
        self._callback = callback
        self.body.setEnabled(False)
        self.message.setText('Выполняется: ' + action)
        self.job = EditorJob(self.workspace, action, arguments, self)
        self.job.result.connect(self.receive)
        self.job.failed.connect(self.failure)
        self.job.progress.connect(self.progress)
        self.job.finished.connect(self.completed)
        self.job.start()

    @Slot(object)
    def receive(self, result):
        self._result = result

    @Slot(str)
    def failure(self, message):
        self.last_error = message
        if self._active_action == 'pilot-run':
            self.pilot_status.setText(message)
        if self._active_action == 'api-models':
            self.api_status.setText(message + '. Авторизация не подтверждена.')

    @Slot(int)
    def progress(self, count):
        if self._active_action == 'pilot-run':
            self.message.setText(f'Luna: checkpoint после seq {count}; можно остановить и продолжить.')
            return
        self.message.setText((f'MockProvider: обработано {count:,} строк' if self._active_action == 'mock-run'
                              else f'QA: проверено {count:,} строк'))
        if self._active_action == 'mock-run':
            self.mock_progress.setText(f'MockProvider: checkpoint после seq {count:,}. Остановка сохранит checkpoint.')

    @Slot()
    def completed(self):
        job, callback, result = self.job, self._callback, self._result
        action = self._active_action
        self.job = None
        job.deleteLater()
        self.body.setEnabled(True)
        self.message.setText(self.last_error or 'Готово')
        if self.closing:
            self.closing = False
            self.close()
            return
        if not self.last_error and callback:
            callback(result)
        elif self.last_error and action.startswith('native-'):
            self.play_failed(action, self.last_error)
        if self.job is None and self._play_pending:
            self._play_pending = False
            self.refresh_play()

    def cancel(self):
        if self.job:
            self.job.cancelled.set()

    def refresh_summary(self):
        if self.job is None:
            self.task('overview', {}, self.show_overview)

    def discover_game(self):
        if self.path_manager is not None:
            self.task('game-discovery', {'data_root': str(self.path_manager.root)},
                      self.show_game_discovery)

    def show_game_discovery(self, result):
        installations = result.get('installations', [])
        self.play_installation = installations[0] if installations else None
        if not installations:
            self.game_status.setText('Игра не обнаружена. Переводы и редактор доступны офлайн.')
        else:
            self.game_status.setText('\n'.join(
                f"{item['path']} · клиент: {item['client_version'] or 'UNKNOWN'} · "
                f"Steam build: {item['steam_build_id'] or 'нет'} · Reader: {item['reader_state']}"
                for item in installations))
        self.reload()
        self.refresh_play()

    def refresh_summary_sync(self):
        from .product import project_overview
        from .translation_db import TranslationDB
        core = TranslationDB(self.workspace)
        try:
            self.show_overview(project_overview(core))
        finally:
            core.close()

    def show_overview(self, report):
        total = report['records']
        sources = report['source_counts']
        public = report['public_sources']
        lines = [f"Активных исходных записей: {total:,}",
                 f"Актуальных переводов: {report['translated']:,}",
                 f"Покрытие: {report['coverage_percent']}%",
                 'Происхождение: ' + (', '.join(f'{name}: {count:,}' for name, count in sorted(sources.items()))
                                       if sources else 'источники пока не импортированы')]
        identities = sorted({(item['repository'], item['revision'], item['data_version']) for item in public})
        if identities:
            lines.append('Публичный корпус (Textmap JSON; это не оригинальные игровые SQLite):')
            lines.extend(f'• {repo} · {version} · commit {revision[:12]}'
                         for repo, revision, version in identities)
        else:
            lines.append('Публичный корпус: пока не подключён.')
        self.home_summary.setText('\n'.join(lines))
        self.database_summary.setText('\n'.join(lines))

    def show_mock_preview(self, result):
        preview = result['preview']
        self.translation_estimate.setText(
            f"Тестовых строк: {result['records']} · запросов Mock: {preview['requests']} · "
            f"UTF-8 proxy оценки: {preview['estimated_input_tokens']:,} bytes · стоимость: $0.00. "
            'Это не токенизатор и не тарифная оценка API; сетевые запросы не выполняются.')

    def show_mock_result(self, result):
        rows = result['rows']
        self.mock_results.setHorizontalHeaderLabels(['English (тест)', 'Mock output · имитация', 'Статус', 'Ручная правка'])
        self.mock_results.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, value in enumerate((row['source'], row['target'] or '', row['status'],
                                             'Да' if row['manual'] else 'Нет')):
                self.mock_results.setItem(index, column, QTableWidgetItem(str(value)))
        self.mock_progress.setText(
            f"Run: {result['run']['state']} · обработано {result['run']['processed']} строк. " +
            'MockProvider: локальная имитация, это не ответ настоящей модели.')

    def build_resource_catalog(self):
        root = QFileDialog.getExistingDirectory(self, 'Выбери корень установки игры (только чтение)')
        if root:
            self.task('resource-catalog', dict(root=Path(root)), self.show_resource_catalog)

    def show_resource_catalog(self, report):
        self.catalog_result.setPlainText(
            f"Пакетов: {report['package_count']:,}\nДоступных записей: {report['available_entries']:,}\n"
            f"JSONL: {report['path']}\nИндекс: {report['index']}\n"
            'Это инвентаризация доступного Reader подмножества. Клиентская версия и load order UNKNOWN; '
            'игровые файлы не изменялись.')

    def choose_project(self):
        if not self.discard():
            return
        path = QFileDialog.getExistingDirectory(self, 'Выбери папку существующего или нового проекта', str(self.workspace.parent))
        if not path:
            return
        try:
            from .translation_db import TranslationDB
            check = TranslationDB(Path(path))
            check.close()
        except Exception as exc:
            QMessageBox.critical(self, 'Не удалось открыть проект', f'{type(exc).__name__}: {exc}')
            return
        QSettings('WuWaRussian', 'WuWaRussian').setValue('last_workspace', str(Path(path).absolute()))
        window = EditorWindow(Path(path), self.path_manager, self.known_roots)
        window.show()
        self.close()

    def open_project_chooser(self):
        if self.path_manager is None or not self.discard():
            return
        chooser = ProjectChooser(self.path_manager, self.known_roots, self)
        if chooser.exec() != QDialog.Accepted or chooser.workspace is None:
            return
        self.path_manager.save_last_project(chooser.workspace)
        window = EditorWindow(chooser.workspace, self.path_manager, self.known_roots)
        window.show()
        self.close()

    def open_workspace(self):
        if sys.platform == 'win32':
            os.startfile(str(self.workspace))
        else:
            QMessageBox.information(self, 'Папка проекта', str(self.workspace))

    def check_project_files(self):
        backup_root = self.path_manager.backups if self.path_manager is not None else None
        self.task('project-files', {'backup_root': str(backup_root) if backup_root else None},
                  self.show_project_files)

    def show_project_files(self, report):
        lines = [f"Проект: {report['workspace']}",
                 f"База: {report['database']} · {int(report['database_bytes']):,} байт",
                 f"Resource Catalog: {report['catalog_count']} файлов · {int(report['catalog_bytes']):,} байт",
                 f"Всего управляемых данных: {int(report['managed_bytes']):,} байт",
                 f"SQLite: {report['integrity']} · активных строк: {report['record_count']:,}",
                 'Экспорты: ' + (', '.join(Path(path).name for path in report['exports']) or 'нет'),
                 'Резервные копии: ' + (', '.join(Path(path).name for path in report['backups']) or 'нет'),
                 'Связанные Resource Catalog: ' + (', '.join(
                     f"{Path(item['path']).name} · " + ('проверен' if item['verified'] else 'недоступен/изменён')
                     for item in report['linked_catalogs']) or 'нет')]
        self.files_summary.setText('\n'.join(lines))

    def backup_project(self):
        if self.path_manager is None:
            QMessageBox.information(self, 'Резервная копия', 'Запусти приложение из portable-папки, чтобы выбрать единый каталог резервных копий.')
            return
        self.task('project-backup', {'data_root': str(self.path_manager.root)}, self.show_project_backup)

    def show_project_backup(self, report):
        QMessageBox.information(self, 'Резервная копия проверена',
            f"Строк: {report['record_count']:,}\nSHA-256: {report['sha256']}\n{report['path']}")
        self.check_project_files()

    def migrate_current_project(self):
        if self.path_manager is None:
            QMessageBox.information(self, 'Расположение проекта', 'Запусти приложение из portable-папки для переноса в Data/Projects.')
            return
        if not self.discard():
            return
        answer = QMessageBox.question(
            self, 'Изменить расположение проекта',
            f'Создать проверенную копию в {self.path_manager.projects}? Исходный проект останется на месте, PAK не копируются.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.task('migrate-workspace',
                      {'data_root': str(self.path_manager.root), 'source': str(self.workspace)},
                      self.open_migrated_workspace)

    def open_migrated_workspace(self, result):
        destination = Path(result['destination'])
        self.path_manager.save_last_project(destination)
        if destination.resolve() == self.workspace.resolve():
            self.message.setText('Проект уже находится в portable-хранилище; совпавшая база переиспользована.')
            return
        window = EditorWindow(destination, self.path_manager, self.known_roots)
        window.show()
        self.close()

    def rollback_workspace(self):
        if self.path_manager is None or not self.discard():
            return
        answer = QMessageBox.question(
            self, 'Откат переноса',
            'Сохранить резервную копию текущих переводов и восстановить настройки до переноса? '
            'Старые и перенесённые данные останутся на месте. Перезапусти приложение после отката.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.task('rollback-workspace', {'data_root': str(self.path_manager.root)},
                      lambda _: self.message.setText('Настройки восстановлены. Закрой приложение; данные сохранены.'))

    def pak_export(self):
        source = QFileDialog.getExistingDirectory(
            self, 'Выбери отдельный offline staging с Client/Content/Aki/ConfigDB/en/lang_*.db')
        if not source:
            return
        name, ok = QInputDialog.getText(
            self, 'Новая PAK-папка', 'Новый относительный путь внутри проекта:',
            text='output/pak-synthetic')
        if ok and name:
            self.task('pak-export', dict(source=Path(source), name=name), self.show_pak_result)

    def show_pak_result(self, result):
        QMessageBox.information(
            self, 'Synthetic PAK собран',
            f"SHA-256: {result['pak_sha256']}\n"
            f"Совместимость: UNKNOWN · установка запрещена\n"
            f"SQLite-файлов: {len(result['databases'])}")

    def dirty(self):
        return self.current is not None and (self.target.toPlainText() != (self.current['row']['target'] or '') or
                                             self.context.text() != self.current['row']['context'])

    def discard(self):
        return not self.dirty() or QMessageBox.question(self, 'Несохранённые правки',
            'Отбросить несохранённые изменения?', QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def reload(self, reset=True):
        if self.job is not None or not self.discard():
            return
        if reset:
            self.pages = [0]
        self.current = None
        self.source.clear()
        self.target.clear()
        self.context.clear()
        args = dict(query=self.search.text(), category=self.category.currentText() if self.category.currentIndex() else '',
                    status=self.status.currentText() if self.status.currentIndex() else '', resource=self.resource.text(),
                    severity=self.severity.currentText() if self.severity.currentIndex() else '', after=self.pages[-1],
                    source_type=self.source_type.currentText() if self.source_type.currentIndex() else '',
                    merge_filter=('', 'CONFLICT', 'CANDIDATES')[self.merge_filter.currentIndex()])
        self.task('page', args, self.show_page)

    def show_page(self, rows):
        self.model.replace(rows)
        self.page_label.setText(f'Страница {len(self.pages)} · {len(rows)} строк (≤200)')
        self.identity.setText('Выбери строку в таблице' if rows else 'По этим условиям записей нет')
        self.diagnostics.clear()
        if getattr(self, '_refresh_overview_after_reload', False):
            self._refresh_overview_after_reload = False
            self.refresh_summary_sync()

    def next_page(self):
        if self.model.rows and self.discard():
            self.current = None
            self.pages.append(self.model.rows[-1]['seq'])
            self.reload(False)

    def previous_page(self):
        if len(self.pages) > 1 and self.discard():
            self.current = None
            self.pages.pop()
            self.reload(False)

    def select_record(self, index, previous):
        if self.job is not None or not index.isValid():
            return
        if not self.discard():
            self.table.selectionModel().blockSignals(True)
            self.table.setCurrentIndex(previous)
            self.table.selectionModel().blockSignals(False)
            return
        seq = self.model.rows[index.row()]['seq']
        self.task('detail', dict(seq=seq), self.show_detail)

    def show_detail(self, detail):
        self.current = detail
        row = detail['row']
        provenance = detail.get('provenance', {})
        self.provenance.setPlainText(row['source_type'] + ' · ' + provenance.get('data_version', 'Версия UNKNOWN') + '\n' +
            provenance.get('repository', '') + ' @ ' + provenance.get('revision', 'UNKNOWN') + '\n' +
            'SHA-256: ' + provenance.get('sha256', 'UNKNOWN') + ' · соответствие игре UNKNOWN\n' +
            json.dumps(provenance, ensure_ascii=False, indent=2) +
            '\nМетаданные записи: ' + row['source_metadata'] + '\nВарианты EN→RU: ' +
            json.dumps(detail.get('merge_candidates', []), ensure_ascii=False, indent=2))
        self.identity.setText(f"{row['resource']} / {row['table_name']} · {row['id_type']} ID={row['id_value']} · "
                              f"seq={row['seq']} · {row['status']} · контекстные ссылки: {detail['links']}")
        self.source.setPlainText(row['source'])
        self.target.setPlainText(row['target'] or '')
        self.context.setText(row['context'])
        self.show_issues(detail['issues'])

    def show_issues(self, issues):
        self.diagnostics.setPlainText('\n'.join(f'{i.severity}: {i.code} {i.detail}' for i in issues) or 'QA: замечаний нет')

    def validate_current(self):
        if self.current:
            issues = TranslationValidator().check(self.current['row']['source'], self.target.toPlainText())
            self.show_issues(issues)
            return not any(i.severity == 'ERROR' for i in issues)
        return False

    def refresh_current(self, _=None):
        if self.current:
            self.task('detail', dict(seq=self.current['row']['seq']), self.update_current)

    def update_current(self, detail):
        self.show_detail(detail)
        # Update visible values and conflict tokens without resetting selection.
        for index, row in enumerate(self.model.rows):
            if row['seq'] == detail['row']['seq']:
                self.model.rows[index] = dict(detail['row'], version=detail['version'])
                self.model.dataChanged.emit(self.model.index(index, 0), self.model.index(index, 6))

    def save(self, approved=False):
        if self.job is not None or not self.current or not self.validate_current():
            return
        self.task('save', dict(seq=self.current['row']['seq'], target=self.target.toPlainText(),
                              context=self.context.text(), expected=self.current['version'],
                              status='APPROVED' if approved else 'REVIEWED'), self.refresh_current)

    def batch_status(self):
        if not self.discard():
            return
        versions = {self.model.rows[i.row()]['seq']: self.model.rows[i.row()]['version']
                    for i in self.table.selectionModel().selectedRows()}
        if not versions:
            return
        status, ok = QInputDialog.getItem(self, 'Пакетный статус', 'Статус выбранных записей:',
                                          ['REVIEWED', 'APPROVED', 'NEEDS_REVIEW', 'INVALID'], 0, False)
        if ok:
            self.current = None
            self.task('statuses', dict(versions=versions, status=status), lambda _: self.reload(False))

    def change_category(self):
        if self.current and self.discard():
            category, ok = QInputDialog.getItem(self, 'Категория', 'Категория строки:', CATEGORIES, 0, False)
            if ok:
                self.task('category', dict(seq=self.current['row']['seq'], category=category,
                                           expected=self.current['version']), self.refresh_current)

    def save_context(self):
        if self.current:
            if self.target.toPlainText() != (self.current['row']['target'] or ''):
                self.message.setText('Нажми «Сохранить»: перевод и контекст сохранятся вместе')
                return
            self.task('context', dict(seq=self.current['row']['seq'], context=self.context.text(),
                                      expected=self.current['version']), self.refresh_current)

    def link_context(self):
        if self.current and self.discard():
            related, ok = QInputDialog.getInt(self, 'Связь контекста', 'Внутренний seq связанной строки:', 1, 1, 2**31-1)
            if ok:
                self.task('link', dict(seq=self.current['row']['seq'], related=related,
                                      expected=self.current['version']), self.refresh_current)

    def show_history(self):
        if not self.current or not self.discard():
            return
        self.history_dialog(self.current['history'])

    def history_dialog(self, entries):
        dialog = QDialog(self)
        dialog.setWindowTitle('История — восстановление создаёт новую правку, без автоутверждения')
        dialog.resize(950, 450)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(entries), 4)
        table.setHorizontalHeaderLabels(['Ревизия', 'Дата', 'Событие', 'Перевод'])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        for index, entry in enumerate(entries):
            for column, value in enumerate((entry['revision'], entry['created'], entry['event'],
                                            json.loads(entry['after_json']).get('target'))):
                table.setItem(index, column, QTableWidgetItem(str(value or '')))
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        def restore():
            index = table.currentRow()
            if index >= 0:
                args = dict(seq=self.current['row']['seq'], revision=entries[index]['revision'], expected=self.current['version'])
                dialog.accept()
                self.task('restore', args, self.refresh_current)
        self.button(layout, 'Восстановить выбранную ревизию', restore)
        if entries:
            def older():
                dialog.accept()
                self.task('history', dict(seq=self.current['row']['seq'], before=entries[-1]['revision']), self.history_dialog)
            self.button(layout, 'Предыдущие 100 ревизий', older)
        dialog.exec()

    def terminology(self):
        term, ok = QInputDialog.getText(self, 'Терминология', 'Английский термин (без глобальной замены):')
        if ok and term:
            self.term = term
            self.task('term', dict(term=term), self.term_dialog)

    def term_dialog(self, result):
        dialog = QDialog(self)
        dialog.setWindowTitle('Термин: ' + self.term)
        dialog.resize(900, 500)
        layout = QVBoxLayout(dialog)
        variants = '\n'.join(f"{r['uses']} × {r['target']}" for r in result['variants'])
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText('Варианты (до 100 за страницу):\n' + variants + '\n\nУпотребления (до 200):\n' +
                          '\n'.join(f"seq={r['seq']} · {r['context']}\n{r['source']} → {r['target']}" for r in result['rows']))
        layout.addWidget(text)
        russian, context = QLineEdit(), QLineEdit()
        category = QComboBox()
        category.addItems(['other', 'proper_name', 'character', 'location', 'ability', 'item'])
        form = QFormLayout()
        form.addRow('Утверждённый вариант:', russian)
        form.addRow('Точный контекст исключения (пусто = общий):', context)
        form.addRow('Тип термина:', category)
        layout.addLayout(form)
        def add():
            args = dict(term=self.term, russian=russian.text(), context=context.text(), category=category.currentText())
            dialog.accept()
            self.task('glossary', args, lambda _: self.message.setText('Утверждённый термин сохранён новой ревизией'))
        self.button(layout, 'Закрепить в глоссарии', add)
        if result['rows']:
            def more():
                dialog.accept()
                self.task('term', dict(term=self.term, after=result['rows'][-1]['seq']), self.term_dialog)
            self.button(layout, 'Следующие 200 употреблений', more)
        if result['variants']:
            def more_variants():
                dialog.accept()
                self.task('term', dict(term=self.term, variant_after=result['variants'][-1]['target']), self.term_dialog)
            self.button(layout, 'Следующие 100 вариантов', more_variants)
        dialog.exec()

    def output(self, action):
        if not self.discard():
            return
        name, ok = QInputDialog.getText(self, 'Новый выходной каталог', 'Относительный путь внутри workspace:', text='output/' + action)
        if ok and name:
            self.task(action, dict(name=name), lambda result: self.show_output_result(action, name, result))

    def show_output_result(self, action, name, result):
        if action == 'report':
            self.qa_summary.setText(
                f"Строк: {result['total']:,} · переведено: {result['translated']:,} · "
                f"не переведено: {result['untranslated_rows']:,} · "
                f"структурных ошибок: {result['error_rows']:,} · предупреждений: {result['WARNING']:,}. "
                'Непереведённые строки не включены в число структурных ошибок.'
            )
            details = (f"строк: {result['total']}, не переведено: {result['untranslated_rows']}, "
                       f"структурных ERROR: {result['error_rows']}, WARNING: {result['WARNING']}")
        else:
            details = f"экспортировано строк: {result['rows']}"
        self.message.setText(f'Готово: {self.workspace / name} · {details}')

    def import_public(self):
        if not self.discard():
            return
        if self.path_manager is not None:
            binding = self.path_manager.public_source('Arikatsu')
            if binding is None:
                root = self.path_manager.root
                lock = root / 'Data/Sources/public-text-sources.json'
                if not lock.is_file():
                    lock = Path(__file__).resolve().parent / 'public-text-sources.json'
                for source_root in (root / 'Data/Sources/Arikatsu', root / 'Imports/Arikatsu'):
                    if lock.is_file() and source_root.is_dir():
                        binding = dict(lock=lock, source_root=source_root)
                        break
            if binding is not None:
                self.connect_public(binding['lock'], binding['source_root'], 'Arikatsu')
                return
        lock, _ = QFileDialog.getOpenFileName(self, 'Lock-файл публичного корпуса', '', 'JSON (*.json)')
        if not lock:
            return
        from .public_corpus import read_lock, source_catalog
        try:
            names = list(source_catalog(Path(lock)))
            source_name, ok = QInputDialog.getItem(self, 'Публичный корпус', 'Источник:', names, 0, False)
            if not ok:
                return
            read_lock(Path(lock), source_name)
        except (ValueError, KeyError, OSError) as exc:
            self.message.setText(str(exc))
            return
        source_root = QFileDialog.getExistingDirectory(self, 'Корень скачанного корпуса (с Textmaps или TextMap)')
        if source_root:
            self.current = None
            if self.path_manager is not None:
                self.connect_public(Path(lock), Path(source_root), source_name)
            else:
                self.task('import-public', dict(lock=Path(lock), source_root=Path(source_root), source_name=source_name),
                          lambda _: self.public_imported())

    def connect_public(self, lock, source_root, source_name):
        self.task('connect-public', dict(data_root=self.path_manager.root, lock=lock,
                  source_root=source_root, source_name=source_name, known_roots=self.known_roots),
                  self.public_connected)

    def public_connected(self, result):
        destination = Path(result['workspace'])
        if destination != self.workspace:
            self.next_window = EditorWindow(destination, self.path_manager, self.known_roots)
            self.next_window.show()
            self.close()
        else:
            self.public_imported()

    def public_imported(self):
        self._refresh_overview_after_reload = True
        self.reload()

    def import_translations(self):
        if not self.discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Разрешённый офлайн EN→RU экспорт', '', 'Данные (*.json *.csv *.db *.sqlite)')
        if not path:
            return
        config, _ = QFileDialog.getOpenFileName(self, 'Явная схема полей и подтверждение прав', '', 'JSON (*.json)')
        if config:
            from .merge import read_config
            try:
                args = dict(path=Path(path), config=read_config(Path(config)))
            except (ValueError, OSError) as exc:
                self.message.setText(str(exc))
                return
            self.task('merge', args, lambda result: self.merge_preview(result, args))

    def merge_preview(self, result, args):
        dialog = QDialog(self)
        dialog.setWindowTitle('Предпросмотр EN→RU — новые переводы потребуют вычитки')
        dialog.resize(950, 650)
        layout = QVBoxLayout(dialog)
        summary = QLabel(f"Строк во входном файле: {result['entries']:,} · К заполнению: {result['eligible_rows']:,}\n"
                         f"Конфликтов: {result['conflict_rows']:,} · Ручных правок защищено: {result['manual_protected_rows']:,}\n"
                         'Ни один существующий перевод не будет заменён. Подробности и первые 100 совпадений ниже.')
        summary.setTextFormat(Qt.PlainText)
        summary.setWordWrap(True)
        layout.addWidget(summary)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        text.setAccessibleName('Предпросмотр импорта: изменения, конфликты и покрытие')
        layout.addWidget(text)
        apply_button = QPushButton('Применить этот предпросмотр')
        apply_button.setObjectName('mergeApply')
        apply_button.clicked.connect(dialog.accept)
        layout.addWidget(apply_button)
        if dialog.exec() == QDialog.Accepted:
            self.current = None
            self.task('merge', dict(**args, apply=True, expected=result['preview_hash']),
                      lambda report: self.merge_applied(report))

    def merge_applied(self, report):
        self.last_import_id = report['import_id']
        self.reload()

    def show_coverage(self, report):
        dialog = QDialog(self)
        dialog.setWindowTitle('Фактическое покрытие и остаток для Luna · без API')
        dialog.resize(700, 450)
        layout = QVBoxLayout(dialog)
        summary = QLabel(f"Заполнено: {report['filled_rows']:,} / {report['total_rows']:,} ({report['filled_percent']}%)\n"
                         f"Вычитано: {report['reviewed_rows']:,} ({report['reviewed_percent']}%)\n"
                         f"Без актуального перевода: {report['remaining_rows']:,} строк / {report['remaining_unique_en']:,} уникальных EN\n"
                         f"В очереди ядра для Luna: {report['luna_queue_rows']:,}. Это счётчик, а не запуск API.")
        summary.setTextFormat(Qt.PlainText)
        layout.addWidget(summary)
        text = QPlainTextEdit(json.dumps(report, ensure_ascii=False, indent=2))
        text.setReadOnly(True)
        layout.addWidget(text)
        dialog.exec()

    def show_candidates(self, *, all_rows=False, after=0):
        seq = None if all_rows else (self.current['row']['seq'] if self.current else None)
        if seq is None and not all_rows:
            return
        self.task('merge-candidates', dict(seq=seq, after=after, include_rolled_back=True),
                  lambda rows: self.candidates_dialog(rows, all_rows))

    def candidates_dialog(self, rows, all_rows=False):
        dialog = QDialog(self)
        dialog.setWindowTitle('Варианты и происхождение · проверка через «Сохранить» / «Утвердить»')
        dialog.resize(1050, 620)
        layout = QVBoxLayout(dialog)
        table = QTableWidget(len(rows), 9)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setHorizontalHeaderLabels(['seq', 'Совпадение', 'Решение', 'Русский', 'Автор', 'Версия', 'Проверка', 'ID импорта', 'Сессия'])
        table.setColumnWidth(3, 260)
        for index, row in enumerate(rows):
            for column, value in enumerate((row['seq'], row['alignment'], row['decision'],
                    row['entry']['target'], row['entry']['author'], row['provenance']['version'],
                    row.get('current_status', ''), row['import_id'], row['state'])):
                item = QTableWidgetItem()
                item.setData(Qt.DisplayRole, str(value))
                item.setToolTip(str(value))
                table.setItem(index, column, item)
        layout.addWidget(table)
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setAccessibleName('Полное происхождение варианта перевода')
        layout.addWidget(detail)
        table.currentCellChanged.connect(lambda r, *unused: detail.setPlainText(
            json.dumps(rows[r], ensure_ascii=False, indent=2)) if r >= 0 else None)
        if rows:
            table.setCurrentCell(0, 0)
        use = QPushButton('Взять вариант в поле редактирования')
        use.setObjectName('mergeUseCandidate')
        use.setEnabled(bool(rows) and not all_rows)
        def choose():
            index = table.currentRow()
            if index >= 0:
                self.target.setPlainText(rows[index]['entry']['target'])
                dialog.accept()
        use.clicked.connect(choose)
        layout.addWidget(use)
        if rows:
            more = QPushButton('Далее →')
            more.clicked.connect(lambda: (dialog.accept(), self.show_candidates(all_rows=all_rows, after=rows[-1]['cursor'])))
            layout.addWidget(more)
        dialog.exec()

    def rollback_merge(self):
        if not self.discard():
            return
        import_id, ok = QInputDialog.getText(self, 'Полный откат импорта',
            'ID сессии из журнала. При последующих правках откат целиком отклоняется:',
            text=getattr(self, 'last_import_id', ''))
        if ok and import_id:
            self.current = None
            self.task('merge-rollback', dict(import_id=import_id), lambda _: self.reload())

    def import_source(self):
        if not self.discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Исходная SQLite из офлайн-staging', '', 'SQLite (*.db)')
        if path:
            resource, ok = QInputDialog.getText(self, 'Имя ресурса', 'Стабильное относительное имя:', text=Path(path).name)
            if ok:
                self.current = None
                self.task('import', dict(resource=resource, source=Path(path)), lambda _: self.reload())

    def closeEvent(self, event):
        if self.job is not None:
            self.closing = True
            self.cancel()
            self.message.setText('Останавливаю операцию перед закрытием…')
            event.ignore()
        elif self.closing or self.discard():
            event.accept()
        else:
            event.ignore()


class ProjectChooser(QDialog):
    """First-run entry point; project paths are selected in the native file picker."""
    def __init__(self, paths: PathManager, known_roots=(), parent=None):
        super().__init__(parent)
        self.workspace = None
        self.paths = paths
        self.migration_job = None
        settings = QSettings('WuWaRussian', 'WuWaRussian')
        old_recent = settings.value('last_workspace', '', type=str)
        if old_recent and paths.last_project() is None:
            paths.save_last_project(Path(old_recent))
        self.setWindowTitle('WuWa Russian — выбрать проект')
        self.resize(740, 500)
        layout = QVBoxLayout(self)
        title = QLabel('WuWa Russian\nВыбери найденный проект или создай новый. Базы проверяются перед показом.')
        title.setWordWrap(True)
        layout.addWidget(title)
        self.candidates = WorkspaceDiscoveryService(paths, known_roots=known_roots).discover()
        self.projects = QListWidget()
        for candidate in self.candidates:
            translated = f'{candidate.translated_count:,} / {candidate.record_count:,}'
            version = f' · DB {candidate.version}' if candidate.version is not None else ''
            catalog_count = len(candidate.catalog_references)
            self.projects.addItem(
                f'{candidate.name}{version} · {translated} строк заполнено · '
                f'{candidate.manual_count:,} ручных · каталогов: {catalog_count}\n'
                f'{candidate.path} · изменён {candidate.modified:%Y-%m-%d %H:%M}')
        self.projects.itemDoubleClicked.connect(lambda _: self.open_selected())
        layout.addWidget(self.projects, 1)
        layout.addWidget(QLabel('Рабочая папка не должна находиться внутри установленной игры.'))
        buttons = QHBoxLayout()
        continue_button = QPushButton('Открыть выбранный проект')
        continue_button.clicked.connect(self.open_selected)
        migrate_button = QPushButton('Перенести выбранный проект в portable-папку…')
        migrate_button.clicked.connect(self.migrate_selected)
        open_button = QPushButton('Открыть существующий проект…')
        open_button.clicked.connect(self.open_existing)
        create_button = QPushButton('Создать проект…')
        create_button.clicked.connect(self.create_project)
        buttons.addWidget(continue_button)
        buttons.addWidget(migrate_button)
        buttons.addWidget(open_button)
        buttons.addWidget(create_button)
        layout.addLayout(buttons)

    def open_existing(self):
        path = QFileDialog.getExistingDirectory(self, 'Папка проекта', str(self.paths.projects))
        if path:
            self.select_workspace(Path(path))

    def open_selected(self):
        index = self.projects.currentRow()
        if 0 <= index < len(self.candidates):
            self.select_workspace(self.candidates[index].path)

    def migrate_selected(self):
        index = self.projects.currentRow()
        if not 0 <= index < len(self.candidates):
            return
        source = self.candidates[index]
        answer = QMessageBox.question(
            self, 'Перенести проект',
            f'Создать проверенную копию проекта «{source.name}» в {self.paths.projects}?\n\n'
            f'Активных строк: {source.record_count:,}; переведено: {source.translated_count:,}. '
            'Будут скопированы база переводов, закреплённый корпус и каталоги. Исходные файлы останутся на месте; PAK не копируются.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.projects.setEnabled(False)
        self.migration_status = QLabel('Создаю резервную копию и проверяю перенос…')
        self.layout().addWidget(self.migration_status)
        self.migration_job = EditorJob(
            self.paths.projects, 'migrate-workspace',
            {'data_root': str(self.paths.root), 'source': str(source.path)}, self)
        self.migration_job.result.connect(self.migration_done)
        self.migration_job.failed.connect(self.migration_failed)
        self.migration_job.start()

    @Slot(object)
    def migration_done(self, result):
        self.migration_job = None
        self.workspace = Path(result['destination'])
        self.paths.save_last_project(self.workspace)
        QMessageBox.information(
            self, 'Перенос завершён',
            f"Проект проверен: {result['record_count']:,} строк.\n"
            f"SHA-256: {result['destination_hash']}\nРезервная копия: {result['backup']}")
        self.accept()

    @Slot(str)
    def migration_failed(self, message):
        self.migration_job = None
        self.projects.setEnabled(True)
        self.migration_status.setText('Перенос не завершён: ' + message)

    def select_workspace(self, path):
        self.workspace = Path(path).absolute()
        self.accept()

    def create_project(self):
        parent = QFileDialog.getExistingDirectory(self, 'Где хранить проект', str(self.paths.projects))
        if not parent:
            return
        name, ok = QInputDialog.getText(self, 'Новый проект', 'Имя папки проекта:', text='WuWa Russian')
        if not ok or not name.strip() or name in ('.', '..') or any(ch in name for ch in '/\\:'):
            return
        destination = Path(parent).absolute() / name.strip()
        try:
            destination.mkdir()
            self.workspace = destination
            self.accept()
        except OSError as exc:
            QMessageBox.critical(self, 'Не удалось создать проект', f'{type(exc).__name__}: {exc}')


def main(argv=None):
    helper_code = helper_main(argv)  # elevated --native-helper mode: no Qt objects at all
    if helper_code is not None:
        return helper_code
    parser = argparse.ArgumentParser(description='WuWa Russian — офлайн-приложение для перевода')
    parser.add_argument('--workspace', type=Path, help='Папка существующего проекта (необязательно)')
    parser.add_argument('--editor', action='store_true',
                        help='Инструменты переводчика (редактор, QA, экспорт) вместо окна игрока')
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from .launcher import LauncherWindow, app_icon
    app.setWindowIcon(app_icon())
    app_root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
    settings = QSettings('WuWaRussian', 'WuWaRussian')
    stored_data_root = settings.value('portable_data_root', '', type=str)
    paths = PathManager(app_root)
    try:
        paths.initialize()
    except OSError:
        pass
    if not paths.is_writable():
        location = stored_data_root or QFileDialog.getExistingDirectory(
            None, 'Выбери место для данных WuWa Russian', str(Path.home()))
        if not location:
            return 0
        paths = PathManager(Path(location) if stored_data_root else Path(location) / 'WuWaRussian-Data')
        try:
            paths.initialize()
        except OSError as exc:
            QMessageBox.critical(None, 'Нет доступа к папке данных', f'{type(exc).__name__}: {exc}')
            return 1
        if not paths.is_writable():
            QMessageBox.critical(None, 'Нет доступа к папке данных', f'Не удалось записывать в {paths.root}')
            return 1
        settings.setValue('portable_data_root', str(paths.root))
    workspace = args.workspace
    recent = paths.last_project()
    if recent is None:
        old_recent = QSettings('WuWaRussian', 'WuWaRussian').value('last_workspace', '', type=str)
        if old_recent:
            recent = Path(old_recent).expanduser().absolute()
            paths.save_last_project(recent)
    known_roots = [app_root / 'Projects']
    if not getattr(sys, 'frozen', False):
        known_roots.append(app_root / 'output')
    if workspace is None and recent is not None:
        choices = WorkspaceDiscoveryService(paths, known_roots=known_roots).discover()
        workspace = next((item.path for item in choices if item.path == recent.resolve(strict=False)), None)
    if workspace is None:
        choices = WorkspaceDiscoveryService(paths, known_roots=known_roots).discover()
        if len(choices) == 1:
            workspace = choices[0].path
    if workspace is None:
        chooser = ProjectChooser(paths, known_roots)
        if chooser.exec() != QDialog.Accepted or chooser.workspace is None:
            return 0
        workspace = chooser.workspace
    workspace = Path(workspace).absolute()
    if paths.needs_self_containment(workspace):
        progress = QDialog()
        progress.setWindowTitle('Подготовка автономного проекта')
        layout = QVBoxLayout(progress)
        layout.addWidget(QLabel('Сохраняю резервную копию, проверяю базу и корпус. Исходные данные остаются на месте…'))
        cancel = QPushButton('Отмена')
        layout.addWidget(cancel)
        job = EditorJob(paths.projects, 'migrate-workspace',
                        dict(data_root=str(paths.root), source=str(workspace)), progress)
        outcome = {}
        job.result.connect(lambda result: (outcome.update(result=result), progress.accept()))
        job.failed.connect(lambda error: (outcome.update(error=error), progress.reject()))
        cancel.clicked.connect(lambda: job.cancelled.set())
        progress.rejected.connect(lambda: job.cancelled.set())
        job.start()
        progress.exec()
        job.cancelled.set()
        job.wait()
        if 'result' not in outcome:
            if 'error' in outcome:
                QMessageBox.critical(None, 'Перенос не завершён', outcome['error'])
            return 1
        workspace = Path(outcome['result']['destination'])
    paths.save_last_project(workspace)
    QSettings('WuWaRussian', 'WuWaRussian').setValue('last_workspace', str(workspace))
    try:
        window = (EditorWindow(workspace, paths, known_roots) if args.editor
                  else LauncherWindow(workspace, paths))
    except (OSError, ValueError, RuntimeError) as exc:
        QMessageBox.critical(None, 'Не удалось открыть проект', f'{type(exc).__name__}: {exc}')
        return 1
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
