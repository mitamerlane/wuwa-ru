"""Product navigation pages composed around the existing editor window."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QSpinBox, QTableWidget, QWidget, QVBoxLayout,
    QScrollArea, QTabWidget, QTableWidgetItem, QHeaderView, QSplitter)

from .play_page import KINDS, kind_label
from .portable import DEFAULT_NATIVE_KINDS
from .product import NATIVE_LIMITATIONS


class ProductPageMixin:
    """Build dashboard pages while leaving the established editor in its own view."""

    def page(self, title, description):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        heading = QLabel(title)
        heading.setStyleSheet('font-size: 23px; font-weight: 700; padding: 7px 0;')
        layout.addWidget(heading)
        note = QLabel(description)
        note.setWordWrap(True)
        note.setStyleSheet('color: #b7c3d2; font-size: 13px; padding-bottom: 8px;')
        layout.addWidget(note)
        return widget, layout

    def make_home_page(self):
        page, layout = self.page('Проект локализации', 'Здесь видно, что подключено, сколько текста уже обработано и какие границы остаются активными.')
        self.home_status = QLabel('\n'.join((NATIVE_LIMITATIONS[0], NATIVE_LIMITATIONS[2], NATIVE_LIMITATIONS[-1])))
        self.home_status.setWordWrap(True)
        self.home_status.setStyleSheet('background:#1b2633; border:1px solid #34465b; border-radius:8px; padding:14px;')
        layout.addWidget(self.home_status)
        self.home_summary = QLabel('Загружаю состояние проекта…')
        self.home_summary.setWordWrap(True)
        self.home_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.home_summary.setStyleSheet('font-size:14px; padding:12px 2px;')
        layout.addWidget(self.home_summary)
        self.game_status = QLabel('Игра не обнаружена; офлайн-проект доступен без установленного клиента.')
        self.game_status.setWordWrap(True)
        layout.addWidget(self.game_status)
        actions = QHBoxLayout()
        self.button(actions, 'Найти / создать проект…', self.open_project_chooser)
        self.button(actions, 'Открыть корпус Arikatsu 3.7…', self.import_public)
        self.button(actions, 'Перейти к редактору', lambda: self.navigate(4))
        layout.addLayout(actions)
        self.button(layout, 'Настроить переводчик', lambda: self.navigate(3))
        self.button(layout, 'Обновить состояние проекта', self.refresh_summary)
        self.button(layout, 'Обнаружить игру (только чтение)', self.discover_game)
        layout.addStretch(1)
        return page

    def make_compatibility_page(self):
        page, layout = self.page(
            'Нативная совместимость',
            'Текущий статус установки русификатора и конкретные причины блокировки.',
        )
        self.compatibility_status = QLabel('\n\n'.join(NATIVE_LIMITATIONS))
        self.compatibility_status.setWordWrap(True)
        self.compatibility_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.compatibility_status.setStyleSheet(
            'background:#1b2633; border:1px solid #34465b; border-radius:8px; padding:14px;'
        )
        layout.addWidget(self.compatibility_status)
        layout.addStretch(1)
        return page

    def make_files_page(self):
        page, layout = self.page(
            'Файлы проекта',
            'Служебные базы и каталоги проекта. Резервная копия содержит проверенный snapshot переводческой SQLite; игровые PAK не копируются.')
        self.files_summary = QLabel('Нажми «Проверить данные», чтобы показать расположение, размер и актуальность файлов.')
        self.files_summary.setWordWrap(True)
        self.files_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.files_summary)
        self.button(layout, 'Открыть папку проекта', self.open_workspace)
        self.button(layout, 'Проверить данные', self.check_project_files)
        self.button(layout, 'Создать резервную копию', self.backup_project)
        self.button(layout, 'Изменить расположение проекта', self.migrate_current_project)
        self.button(layout, 'Откатить перенос проекта…', self.rollback_workspace)
        layout.addStretch(1)
        return page

    def make_resources_page(self):
        page, layout = self.page('Игровые ресурсы', 'Reader и ResourceCatalog работают только на чтение. Индекс показывает доступное подмножество; он не определяет порядок загрузки игры.')
        self.resource_state = QLabel('Reader: частичная поддержка. Закрытые игровые индексы и payload вне подтверждённого подмножества могут оставаться недоступными.')
        self.resource_state.setWordWrap(True)
        layout.addWidget(self.resource_state)
        self.button(layout, 'Проверить каталог установки…', self.build_resource_catalog)
        self.button(layout, 'Импортировать Arikatsu 3.7…', self.import_public)
        self.button(layout, 'Импортировать SQLite из offline staging…', self.import_source)
        self.catalog_result = QPlainTextEdit()
        self.catalog_result.setReadOnly(True)
        self.catalog_result.setPlaceholderText('Результат read-only инвентаризации появится здесь.')
        layout.addWidget(self.catalog_result, 1)
        return page

    def make_database_page(self):
        page, layout = self.page('База переводов', 'Переводы и история хранятся в проекте. Ручные правки защищены от импорта и автоматических результатов.')
        self.database_summary = QLabel('Состояние проекта загружается…')
        self.database_summary.setWordWrap(True)
        layout.addWidget(self.database_summary)
        self.button(layout, 'Найти записи в редакторе', lambda: self.navigate(4))
        self.button(layout, 'Терминология…', self.terminology)
        self.button(layout, 'Импортировать варианты EN→RU…', self.import_translations)
        self.button(layout, 'Журнал вариантов и происхождения…', lambda: self.show_candidates(all_rows=True))
        self.button(layout, 'Откатить последний импорт…', self.rollback_merge)
        layout.addStretch(1)
        return page

    def make_translation_page(self):
        page, layout = self.page('Автоматический перевод', 'Настрой провайдера и модель. Сеть для перевода — только для собственного synthetic пилота после предпросмотра и подтверждения оплаты. Игровой корпус остаётся локальным.')
        outer_layout = layout
        self.translation_tabs = QTabWidget()
        outer_layout.addWidget(self.translation_tabs, 1)
        api_page = QWidget()
        layout = QVBoxLayout(api_page)
        self.translation_tabs.addTab(api_page, 'Ответы API · реальные модели')
        self.pilot_report = None
        pilot_actions = QHBoxLayout()
        self.pilot_source = QComboBox()
        self.pilot_source.addItems(['Собственные synthetic · пул 200 строк', 'Английский корпус · 40 × 5 категорий · только локально'])
        pilot_actions.addWidget(self.pilot_source)
        self.pilot_preview_button = self.button(pilot_actions, 'Подготовить / обновить пилот', self.prepare_api_pilot)
        self.pilot_run_button = self.button(pilot_actions, 'Запустить / продолжить пилот API…', self.confirm_pilot)
        self.pilot_run_button.setEnabled(False)
        self.pilot_source.currentIndexChanged.connect(self.clear_pilot)
        layout.addLayout(pilot_actions)
        limits_form = QFormLayout()
        for name, label, maximum, default in (
                ('pilot_requests', 'Всего попыток (включая продолжения):', 24, 1),
                ('pilot_rows', 'Максимум строк пилота:', 200, 5),
                ('pilot_tokens', 'Выходных токенов на запрос:', 8192, 1024)):
            widget = QSpinBox()
            widget.setRange(1, maximum)
            widget.setValue(default)
            widget.setAccessibleName(label)
            widget.valueChanged.connect(self.clear_pilot)
            setattr(self, name, widget)
            limits_form.addRow(label, widget)
        layout.addLayout(limits_form)
        self.pilot_status = QLabel('Новый платный запуск требует предпросмотра и отдельного подтверждения оплаты.')
        self.pilot_status.setWordWrap(True)
        layout.addWidget(self.pilot_status)
        self.button(layout, 'Показать сохранённые ответы · без сети', self.load_pilot_results)
        self.api_result_summary = QLabel('Сохранённые ответы ещё не загружены. Просмотр не отправляет запросов.')
        self.api_result_summary.setTextFormat(Qt.PlainText)
        self.api_result_summary.setWordWrap(True)
        layout.addWidget(self.api_result_summary)
        self.api_result_rows = []
        self.api_results = QTableWidget(0, 4)
        self.api_results.setHorizontalHeaderLabels(['Английский оригинал', 'Русский ответ модели', 'QA ответа', 'Сохранение в TranslationDB'])
        self.api_results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.api_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.api_results.setSelectionMode(QAbstractItemView.SingleSelection)
        for column in range(4):
            self.api_results.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.Stretch if column < 2 else QHeaderView.ResizeToContents)
        self.api_results.currentCellChanged.connect(self.select_api_result)
        self.api_results.setAccessibleName('Ответы реального API; исходник, ответ, QA и сохранение')
        self.api_detail = QPlainTextEdit()
        self.api_detail.setReadOnly(True)
        self.api_detail.setAccessibleName('Полный ответ модели для выбранной строки')
        self.api_detail.setPlaceholderText('Выбери строку: здесь появятся полный оригинал, ответ модели и текущий перевод.')
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.api_results)
        splitter.addWidget(self.api_detail)
        layout.addWidget(splitter, 1)
        self.api_approve_button = self.button(layout, 'Утвердить выбранный ответ и сохранить', self.approve_api_result)
        self.api_approve_button.setEnabled(False)
        self.pilot_json = QPlainTextEdit()
        self.pilot_json.setReadOnly(True)
        self.pilot_json.setPlaceholderText('Здесь появятся точные запросы, соответствие ID, контекст, глоссарий и стоимость. Ключ API не читается при предпросмотре.')
        self.pilot_json.setMaximumHeight(130)
        self.pilot_json.hide()
        self.button(layout, 'Показать / скрыть технический отчёт', lambda: self.pilot_json.setVisible(self.pilot_json.isHidden()))
        layout.addWidget(self.pilot_json)
        provider_box = QWidget()
        self.add_provider_settings(QVBoxLayout(provider_box))
        self.translation_tabs.addTab(provider_box, 'Настройки API')
        mock_page = QWidget()
        layout = QVBoxLayout(mock_page)
        self.translation_tabs.addTab(mock_page, 'MockProvider · имитация без сети')
        layout.addWidget(QLabel('Локальная структурная демонстрация. Текст ниже не является ответом модели API.'))
        form = QFormLayout()
        self.provider_choice = QComboBox()
        self.provider_choice.addItems(['MockProvider · без сети'])
        self.model_choice = QComboBox()
        self.model_choice.addItems(['mock-v1 · тестовый структурный ответ'])
        form.addRow('Провайдер:', self.provider_choice)
        form.addRow('Модель:', self.model_choice)
        layout.addLayout(form)
        self.translation_estimate = QLabel('Нажми «Оценить», чтобы увидеть число строк, запросов и UTF-8 proxy оценки. Для Mock стоимость — $0.00.')
        self.translation_estimate.setWordWrap(True)
        layout.addWidget(self.translation_estimate)
        budget = QHBoxLayout()
        self.mock_request_limit = QSpinBox()
        self.mock_request_limit.setRange(1, 1000)
        self.mock_request_limit.setValue(10)
        self.mock_request_limit.setToolTip('Ограничивает число batch-вызовов локального Mock run. Платные провайдеры в PRODUCT-1 выключены.')
        budget.addWidget(QLabel('Лимит batch-вызовов:'))
        budget.addWidget(self.mock_request_limit)
        budget.addWidget(QLabel('Расходы Mock: $0.00 · без сети'))
        budget.addStretch(1)
        layout.addLayout(budget)
        actions = QHBoxLayout()
        self.button(actions, 'Оценить', lambda: self.task(
            'mock-preview', {'max_requests': self.mock_request_limit.value()}, self.show_mock_preview))
        self.button(actions, 'Запустить / продолжить Mock', lambda: self.task(
            'mock-run', {'max_requests': self.mock_request_limit.value()}, self.show_mock_result))
        layout.addLayout(actions)
        self.mock_progress = QLabel('Прогресс появится после запуска. Остановка и повторный запуск продолжают тот же тестовый run.')
        self.mock_progress.setWordWrap(True)
        layout.addWidget(self.mock_progress)
        self.mock_results = QTableWidget(0, 4)
        self.mock_results.setHorizontalHeaderLabels(['English (тест)', 'Mock output', 'Статус', 'Ручная правка'])
        self.mock_results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.mock_results, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def prepare_api_pilot(self):
        from dataclasses import asdict
        if self.job is not None:
            return
        profile = self.save_api_settings()
        if profile is not None:
            self.task('pilot-preview', {'public': self.pilot_source.currentIndex() == 1,
                      'profile': asdict(profile), **self.pilot_limits()}, self.show_pilot_preview)

    def pilot_limits(self):
        return dict(max_requests=self.pilot_requests.value(), max_rows=self.pilot_rows.value(),
                    max_output_tokens=self.pilot_tokens.value())

    def clear_pilot(self, *_):
        self.pilot_report = None
        self.pilot_run_button.setEnabled(False)

    def show_pilot_preview(self, report):
        import json
        try:
            current = self.current_api_profile()
        except ValueError:
            return
        if (report.get('provider') != current.name or report.get('base_url') != current.base_url or
                report['model'] != current.model or
                any(report['limits'].get(k) != v for k, v in self.pilot_limits().items())):
            self.pilot_status.setText('Профиль изменён во время подготовки. Обнови предпросмотр.')
            return
        self.pilot_report = report
        estimate = report['preview']
        def money(value):
            return 'Стоимость неизвестна' if value is None else f'${value:.4f}'
        self.pilot_status.setText(
            f"{report.get('provider', 'OpenAI')} / {report['model']} · {report['records']} строк · {estimate['requests']} запросов · "
            f"оценка {money(estimate['estimated_cost'])}, с повторами {money(estimate['estimated_cost_with_retries'])}. "
            f"Это UTF-8 proxy, не точный токенизатор. Глоссарий: {report['glossary_matches']}/{report['records']}; "
            f"дополнительный контекст: {report['semantic_context_known']}/{report['records']}. "
            f"Коэффициент: {report['pricing'].get('coefficient') or 'UNKNOWN'} (не USD). "
            f"Попытки: {report['budget_usage']['attempts_used']}/{report['limits']['max_requests']}. "
            + ('Лимит исчерпан: увеличь общее число попыток и обнови preview. ' if
               report['budget_usage']['attempts_remaining'] == 0 and estimate['requests'] else '') +
            f"Локальные лимиты не гарантируют денежный потолок. Права: {report['rights']}. Отчёт: {report['report_path']}")
        self.pilot_json.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))
        if 'results' in report:
            self.show_api_results(report['results'])
        self.pilot_run_button.setEnabled(report['source_type'] == 'SYNTHETIC_FIXTURE' and estimate['requests'] > 0
                                        and report['limits']['max_requests'] > 0)

    def load_pilot_results(self):
        from dataclasses import asdict
        try:
            profile = self.current_api_profile()
        except ValueError as exc:
            self.api_result_summary.setText(str(exc))
            return
        self.task('pilot-results', {'profile': asdict(profile), **self.pilot_limits()}, self.show_api_results)

    def show_api_results(self, result):
        self.api_result_context = result
        self.api_result_rows = result['rows']
        def tokens(key):
            values = [(u['actual_usage'] or {}).get(key) for u in result['usage']]
            return str(sum(values)) if values and all(type(v) is int for v in values) else 'UNKNOWN'
        usage_lines = []
        for usage in result['usage']:
            actual = usage['actual_usage'] or {}
            input_tokens = actual.get('input_tokens')
            output_tokens = actual.get('output_tokens')
            usage_lines.append(f"#{usage['attempt']}: {usage['state']} · модель {actual.get('response_model') or actual.get('model') or 'UNKNOWN'} · "
                f"input {input_tokens if type(input_tokens) is int else 'UNKNOWN'} / output {output_tokens if type(output_tokens) is int else 'UNKNOWN'}")
        self.api_result_summary.setText(
            f"{result['provider']} / {result['model']} · задание: {(result['run'] or {}).get('state', 'не найдено')}\n"
            f"Фактические токены: input {tokens('input_tokens')} / output {tokens('output_tokens')} · запросов: {len(result['usage'])}\n"
            + '\n'.join(usage_lines) + '\nБаза: .pilot-synthetic · SYNTHETIC_FIXTURE; основной корпус не изменяется.\n'
            + 'Таблица: последние сохранённые ответы этого профиля для выбранных строк.\n'
            + ('Ответы автоматически сохранены как машинные; утверждение защищает строку от автозамены.' if result['rows'] else
               'Сохранённых ответов API для выбранных провайдера, модели и лимитов нет.'))
        self.api_result_summary.setToolTip(result['workspace'])
        self.api_results.setRowCount(0)
        self.api_results.setRowCount(len(result['rows']))
        self.api_detail.clear()
        self.api_approve_button.setEnabled(False)
        for index, row in enumerate(result['rows']):
            state = ('Утверждён · APPROVED' if row['status'] == 'APPROVED' else
                     'Ручная правка защищена' if row['manual'] else
                     'Сохранён · INVALID' if row['status'] == 'INVALID' else 'Сохранён · машинный')
            for column, value in enumerate((row['source'], row['model_target'] or '', '\n'.join(row['qa']) or 'OK', state)):
                self.api_results.setItem(index, column, QTableWidgetItem(str(value)))
        if result['rows']:
            self.api_results.setCurrentCell(0, 0)

    def select_api_result(self, row, *_):
        if not 0 <= row < len(self.api_result_rows):
            self.api_approve_button.setEnabled(False)
            return
        item = self.api_result_rows[row]
        self.api_detail.setPlainText(f"English · {item['id_value']}\n{item['source']}\n\nПолный ответ модели\n{item['model_target'] or ''}\n\n"
            f"QA ответа: {', '.join(item['qa']) or 'OK'}\n\nТекущий перевод в базе · {item['status']}\n{item['target'] or ''}")
        self.api_approve_button.setEnabled(item['can_approve'])

    def approve_api_result(self):
        index = self.api_results.currentRow()
        if not 0 <= index < len(self.api_result_rows):
            return
        row = self.api_result_rows[index]
        if not row['can_approve']:
            return
        # Bind approval to the displayed result, even if the settings tab changed.
        context = self.api_result_context
        profile = context['profile']
        self.task('pilot-approve', dict(profile=profile, seq=row['seq'], expected=row['version'],
            revision=row['revision'], **context['limits']), self.show_api_results)

    def confirm_pilot(self):
        report = self.pilot_report
        if not report or report['source_type'] != 'SYNTHETIC_FIXTURE':
            return
        from dataclasses import asdict
        try:
            profile = self.current_api_profile()
        except ValueError as exc:
            self.pilot_status.setText(str(exc))
            return
        answer = QMessageBox.question(self, 'Подтверждение платного пилота',
            f"Отправить только собственные synthetic строки?\n{report.get('provider', 'OpenAI')} / {report['model']}\n"
            f"{report.get('base_url', 'https://api.openai.com/v1')}\n" +
            ('Стоимость неизвестна. Возможны платные списания.\n' if report['preview']['estimated_cost'] is None else
             f"Оценка USD: ${report['preview']['estimated_cost']:.4f}; это не фактический счёт.\n") +
            f"Лимиты: {report['limits']['max_requests']} попыток всего, {report['records']} строк, "
            f"{report['limits']['max_output_tokens']} выходных токенов/запрос. Автоповторов нет.\n"
            'Локальные лимиты не гарантируют денежный потолок расходов.\n'
            'Установи лимит API-ключа на стороне RelayModels (для других API — у своего провайдера).\n'
            'Остановка сохраняет завершённые batches; текущий запрос может быть оплачен.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.task('pilot-run', {'confirmed': True, 'expected': report['preview_hash'],
                      'profile': asdict(profile), **self.pilot_limits()}, self.show_pilot_preview)

    def make_qa_page(self):
        page, layout = self.page('Проверка качества', 'QA ищет сломанные placeholders, неподдержанные символы и другие проблемы. Отчёт сохраняется в выбранный проект.')
        self.qa_summary = QLabel('Непереведённые строки учитываются отдельно от структурных ошибок.')
        self.qa_summary.setWordWrap(True)
        layout.addWidget(self.qa_summary)
        self.button(layout, 'Сформировать полный отчёт QA…', lambda: self.output('report'))
        self.button(layout, 'Открыть редактор с уровнем QA', lambda: self.navigate(4))
        layout.addStretch(1)
        return page

    def make_export_page(self):
        page, layout = self.page('Экспорт', 'Офлайн-экспорт остаётся доступен. Только проверенные отдельные SQLite-копии; установка в игру заблокирована.')
        self.button(layout, 'Экспортировать SQLite…', lambda: self.output('export'))
        self.button(layout, 'Сформировать отчёт QA…', lambda: self.output('report'))
        self.button(layout, 'Собрать synthetic plain V12 PAK из staging…', self.pak_export)
        self.export_note = QLabel('Экспорт не изменяет игровые файлы. Public Textmap нельзя выдать за оригинальную игровую SQLite. PAK Writer принимает только проверенный offline staging и ограниченный synthetic plain V12 профиль; совместимость UNKNOWN, установка заблокирована.')
        self.export_note.setWordWrap(True)
        layout.addWidget(self.export_note)
        layout.addStretch(1)
        return page

    def make_settings_page(self):
        page, layout = self.page('Настройки проекта', 'Все данные проекта находятся в локальной папке. Пилот Luna требует предпросмотра и явного подтверждения оплаты.')
        self.settings_path = QLabel(str(self.workspace))
        self.settings_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.settings_path.setWordWrap(True)
        layout.addWidget(QLabel('Рабочая папка:'))
        layout.addWidget(self.settings_path)
        self.button(layout, 'Открыть другой проект…', self.choose_project)
        self.button(layout, 'Открыть папку проекта', self.open_workspace)
        layout.addWidget(QLabel('API: раздел «Автоматический перевод». Ключи — Windows Credential Manager; в portable-папку не входят.'))
        heading = QLabel('Что подключать в игру')
        heading.setStyleSheet('font-size: 17px; font-weight: 600; padding-top: 14px;')
        layout.addWidget(heading)
        note = QLabel('Проверены на экранах игры только имена и интерфейс. Остальные волны можно включить '
                      'на свой риск; изменение набора пересоберёт пакет при следующем «Играть».')
        note.setWordWrap(True)
        layout.addWidget(note)
        chosen = self.path_manager.native_kinds() if self.path_manager is not None else DEFAULT_NATIVE_KINDS
        self.native_kind_boxes = {}
        for kind in KINDS:
            box = QCheckBox(kind_label(kind))
            box.setChecked(kind in chosen)
            box.setEnabled(self.path_manager is not None)
            box.toggled.connect(lambda checked=False: self.save_native_kinds())
            self.native_kind_boxes[kind] = box
            layout.addWidget(box)
        self.button(layout, 'Подключить входы…', self.connect_native_inputs)
        self.native_inputs_status = QLabel('Английская таблица и шрифт копируются в Data/Native только '
                                           'после проверки закреплённых SHA-256.')
        self.native_inputs_status.setWordWrap(True)
        layout.addWidget(self.native_inputs_status)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def save_native_kinds(self):
        if self.path_manager is None:
            return
        self.path_manager.save_native_kinds(kind for kind in KINDS if self.native_kind_boxes[kind].isChecked())
        self.refresh_play()

    def connect_native_inputs(self):
        if self.path_manager is None:
            self.native_inputs_status.setText('Входы подключаются только в portable-режиме.')
            return
        if self.job is not None:
            self.play_status.setText('Подожди: идёт другая операция.')
            return
        table, _ = QFileDialog.getOpenFileName(
            self, 'Английская таблица lang_multi_text.en.db (закреплённый snapshot)', '', 'SQLite (*.db)')
        if not table:
            return
        font, _ = QFileDialog.getOpenFileName(self, 'Шрифт NotoSans-600.ttf', '', 'TrueType (*.ttf)')
        if not font:
            return
        self.task('native-inputs', dict(native=str(self.path_manager.native), table=table, font=font),
                  self.native_inputs_connected)

    def native_inputs_connected(self, result):
        error = (result or {}).get('error')
        text = f'Входы не подключены, ничего не скопировано: {error}' if error else             'Входы проверены по SHA-256 и скопированы в Data/Native.'
        self.native_inputs_status.setText(text)
        self.play_status.setText(text)
        self.refresh_play()

    def navigate(self, index):
        """Show a page by its stack index; the tab bar follows (hidden tab for pages from «Ещё»)."""
        if not 0 <= index < self.stack.count():
            return
        self.stack.setCurrentIndex(index)
        pages = [page for _, page in self.tab_pages]
        position = pages.index(index) if index in pages else len(pages)
        blocked = self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(position)
        self.tabs.blockSignals(blocked)
        if index == 3 and self.job is None:
            self.load_pilot_results()
