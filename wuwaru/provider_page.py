"""API settings widgets; workers receive only secret-free profile snapshots."""
from dataclasses import asdict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox)

from .portable import PathManager
from .provider_config import PROVIDERS, ProviderProfile, ProviderSettings, WindowsCredentials


class ProviderPageMixin:
    def add_provider_settings(self, layout):
        paths = self.path_manager or PathManager(self.workspace)
        paths.initialize()
        self.api_settings = ProviderSettings(paths)
        form = QFormLayout()
        self.api_provider = QComboBox()
        self.api_provider.addItems(PROVIDERS)
        self.api_provider.setAccessibleName('API провайдер')
        self.api_url = QLineEdit()
        self.api_url.setAccessibleName('API Base URL')
        self.api_model = QComboBox()
        self.api_model.setEditable(True)
        self.api_model.setAccessibleName('API модель или ручной идентификатор')
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_key.setPlaceholderText('Введи новый ключ; сохранённый ключ не показывается')
        self.api_key.setAccessibleName('Новый API ключ выбранного провайдера')
        self.api_input_price, self.api_output_price = QLineEdit(), QLineEdit()
        for widget in (self.api_input_price, self.api_output_price):
            widget.setPlaceholderText('UNKNOWN — введи USD / 1M tokens')
        for label, widget in (('API провайдер:', self.api_provider), ('Base URL:', self.api_url),
                              ('Модель (можно вручную):', self.api_model), ('API ключ:', self.api_key),
                              ('Цена входа USD / 1M:', self.api_input_price),
                              ('Цена выхода USD / 1M:', self.api_output_price)):
            form.addRow(label, widget)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.button(actions, 'Сохранить настройки / ключ', self.save_api_settings)
        self.button(actions, 'Удалить сохранённый ключ', self.delete_api_key)
        self.button(actions, 'Проверить конфигурацию', self.check_api_config)
        layout.addLayout(actions)
        actions = QHBoxLayout()
        self.api_check_button = self.button(actions, 'Проверить подключение', self.check_api_connection)
        self.api_models_button = self.button(actions, 'Обновить список моделей', self.check_api_connection)
        layout.addLayout(actions)
        self.api_status = QLabel()
        self.api_status.setWordWrap(True)
        self.api_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.api_status)
        self.api_provider.setCurrentText(self.api_settings.selected)
        self.load_api_profile()
        self.api_provider.currentTextChanged.connect(self.load_api_profile)
        self.api_model.currentTextChanged.connect(self.api_model_changed)
        for field in (self.api_url, self.api_input_price, self.api_output_price, self.api_key):
            field.textChanged.connect(self.clear_pilot)
        self.api_url.textEdited.connect(self.api_endpoint_changed)

    def api_endpoint_changed(self, *_):
        self.api_input_price.clear()
        self.api_output_price.clear()
        self.api_status.setText('Base URL изменён: ключ и тарифы привязаны к адресу. Авторизация не проверена.')

    def current_api_profile(self):
        def price(widget):
            text = widget.text().strip()
            try:
                return None if not text or text.upper() == 'UNKNOWN' else float(text.replace(',', '.'))
            except ValueError:
                raise ValueError('Тариф: введи число USD / 1M или оставь UNKNOWN') from None
        relay = self.api_provider.currentText() == 'RelayModels'
        profile = ProviderProfile(self.api_provider.currentText(), self.api_url.text().strip(),
            self.api_model.currentText().strip(), None if relay else price(self.api_input_price),
            None if relay else price(self.api_output_price))
        profile.validate()
        return profile

    def load_api_profile(self, *_):
        self.clear_pilot()
        try:
            profile = self.api_settings.profile(self.api_provider.currentText())
            self.api_url.setText(profile.base_url)
            self.api_url.setReadOnly(profile.name != 'Custom OpenAI-compatible')
            self.api_model.blockSignals(True)
            self.api_model.clear()
            self.api_model.addItem(profile.model)
            self.api_model.blockSignals(False)
            self.set_api_prices(profile)
            self.api_key.clear()
            if profile.base_url:
                self.api_settings.save(profile)
                present = bool(profile.key())
            else:
                present = False
            self.api_status.setText('Ключ: ' + ('есть' if present else 'отсутствует') +
                '. Авторизация: не проверена. Сеть не использовалась.')
        except (ValueError, RuntimeError, OSError):
            self.api_status.setText('Хранилище/профиль недоступны. Укажи параметры и сохрани настройки.')

    def set_api_prices(self, profile):
        relay = profile.name == "RelayModels"
        custom = profile.name == "Custom OpenAI-compatible"
        self.api_input_price.setEnabled(custom)
        self.api_output_price.setEnabled(custom)
        from .billing import pricing
        prices = pricing(profile)
        for widget, value in ((self.api_input_price, profile.input_price),
                              (self.api_output_price, profile.output_price)):
            if not custom:
                value = prices['input' if widget is self.api_input_price else 'output']
            widget.setPlaceholderText('Не применяется — баланс RelayModels' if relay else
                                      'Стоимость неизвестна' if not custom else 'UNKNOWN — введи USD / 1M tokens')
            widget.setText('' if value is None else str(value))

    def api_model_changed(self, model):
        self.clear_pilot()
        profile = self.api_settings.profile(self.api_provider.currentText(), model=model,
                                            base_url=self.api_url.text().strip())
        self.set_api_prices(profile)
        self.api_status.setText('Модель изменена. Авторизация и доступность модели: не проверены.')

    def save_api_settings(self):
        try:
            profile = self.current_api_profile()
            self.api_settings.save(profile)
            if self.api_key.text():
                WindowsCredentials().set(profile.credential_target, self.api_key.text().strip())
                self.api_key.clear()
            self.api_status.setText('Настройки сохранены. Ключ: ' + ('есть' if profile.key() else 'отсутствует') +
                                    '. Авторизация: не проверена; сеть не использовалась.')
            return profile
        except (ValueError, RuntimeError, OSError) as exc:
            self.api_status.setText(str(exc))
            return None

    def delete_api_key(self):
        try:
            profile = self.current_api_profile()
            WindowsCredentials().delete(profile.credential_target)
            self.api_key.clear()
            self.api_status.setText('Сохранённый ключ удалён. ' +
                ('Legacy WUWARU_API_KEY всё ещё доступна из окружения.' if profile.key() else 'Ключ отсутствует.'))
        except (ValueError, RuntimeError, OSError) as exc:
            self.api_status.setText(str(exc))

    def check_api_config(self):
        try:
            profile = self.current_api_profile()
            present = bool(self.api_key.text().strip() or profile.key())
            self.api_status.setText('Конфигурация: ' + ('готова' if present else 'API-ключ отсутствует') +
                '. Стоимость: ' + ('Стоимость неизвестна (UNKNOWN)' if profile.name == 'RelayModels' or None in (profile.input_price, profile.output_price) else 'пользовательские тарифы') +
                '. Авторизация не проверялась; сетевых запросов нет.')
        except (ValueError, RuntimeError, OSError) as exc:
            self.api_status.setText(str(exc))

    def check_api_connection(self):
        if self.job is not None:
            return
        profile = self.save_api_settings()
        if profile is None:
            return
        self.api_status.setText('GET /models… Перевод не запускается.')
        self.task('api-models', {'profile': asdict(profile)}, self.show_api_models)

    def show_api_models(self, result):
        # Ignore results for a profile edited while the worker was running.
        try:
            current = self.current_api_profile()
        except ValueError:
            return
        if result['profile'] != asdict(current):
            self.api_status.setText('Профиль изменён. Проверь подключение ещё раз.')
            return
        models = result['models']
        self.api_model.blockSignals(True)
        self.api_model.clear()
        self.api_model.addItems(models)
        self.api_model.setCurrentText(current.model)
        self.api_model.blockSignals(False)
        self.api_status.setText('Авторизация: подтверждена GET /models. ' +
            (f'Модель {current.model} найдена.' if current.model in models else
             f'Модель {current.model} недоступна в списке; автозамена выключена.') +
            ' Поддержка перевода этой моделью пока не проверена.')
