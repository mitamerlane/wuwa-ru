# Сборка

Нужны Windows 10/11 и Python 3.12.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[build]"   # cryptography, PySide6, PyInstaller
```

## Запуск из исходников

```powershell
.venv\Scripts\python.exe -m wuwaru.gui            # лаунчер
.venv\Scripts\python.exe -m wuwaru.gui --editor   # инструменты переводчика
```

Данные (проекты, настройки, пакет перевода) кладутся в `Data\` рядом с программой;
при запуске из исходников — в корень репозитория (папка в `.gitignore`).

## Тесты

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m unittest discover -s gui_tests -v   # Qt offscreen
```

Тесты работают только на синтетических данных в `.test-tmp\`: игра, Steam, сеть и
настоящие игровые файлы не нужны и не трогаются. Один тест создаёт junction
(`mklink /J`) — в песочнице без этого права он упадёт, это ограничение среды.

## Готовый перевод (необязательно)

Установщик может сразу нести перевод. Нужны проект перевода (`translations.sqlite`),
закреплённая английская таблица и шрифт Noto Sans 600 (оба проверяются по SHA-256):

```powershell
.venv\Scripts\python.exe tools\build_translation_bundle.py `
    --project-db ПРОЕКТ\translations.sqlite --table lang_multi_text.db --font NotoSans-600.ttf
```

Результат — `build\bundle\seed\`. Исходный проект открывается только на чтение.

## Установщик

```powershell
.venv\Scripts\python.exe tools\make_icons.py        # .ico из packaging\icons\*.svg (если меняли)
.venv\Scripts\python.exe tools\build_installer.py   # → dist\WuWaRussian-Setup-<версия>.exe + .sha256
```

`build_installer.py` замораживает приложение (`packaging\WuWaRussian.spec`), упаковывает
его вместе с `build\bundle` (если есть) и собирает однофайловый установщик и
`Uninstall.exe`. Фон окна установщика берётся из локального кэша Steam или задаётся
`--background кадр.jpg`; без кадра рисуется запасной фон. В Git картинки не хранятся.

## Релиз

См. [UPDATES.md](UPDATES.md#как-выпустить-версию): подпись `update.json` и публикация
в GitHub Releases командой `python -m tools.release`.

## Публичный репозиторий

Этот репозиторий собирается из рабочего командой `python -m tools.public_export --out ПАПКА`:
копируются только код приложения, установщик, сборка, нужные инструменты и их тесты,
сверху кладётся папка `public\` (этот README и docs). Перед записью каждый файл
проверяется на ключи, токены, приватные ключи и личные пути; одно совпадение
останавливает экспорт.
