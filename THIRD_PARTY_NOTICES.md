# Сторонние компоненты и происхождение

Код проекта — GPL-3.0-or-later, полный текст в [LICENSE](LICENSE).

## Включено в программу

| Компонент | Где | Лицензия |
|---|---|---|
| Запись архивов PAK — адаптация `wuwa_pak.py` из [GishReloaded/WuWaRu](https://github.com/GishReloaded/WuWaRu) @ `ee6c16ed53e274cff1aa1127ee1c1d85c5a12158` (SHA-256 исходника `106ff62a…ab6a755`) | `wuwaru/pak_builder.py` | GPL-3.0-or-later. Авторство — участники GishReloaded/WuWaRu по истории репозитория; исходник ссылается на FabianFG/CUE4Parse и Arkael-Dev/WuwaIDLauncher (GPL-3.0). Изменения проекта: ограниченный вход, защита путей и SQLite, отмена, атомарная запись, независимая проверка. Производная работа, не clean-room |
| [cryptography](https://github.com/pyca/cryptography) 49.0.0 (вместе с OpenSSL, cffi, pycparser) | AES индекса архива, Ed25519 обновлений | Apache-2.0 OR BSD-3-Clause; cffi — MIT; pycparser — BSD-3-Clause |
| [PySide6 / Qt for Python](https://pypi.org/project/PySide6/6.10.2/) 6.10.2, Shiboken6 | интерфейс | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| [PyInstaller](https://pyinstaller.org/) | сборка EXE | GPL-2.0-or-later с исключением для собранных программ |
| Шрифт [Unbounded](https://github.com/googlefonts/unbounded) `[wght]`, © 2022 The Unbounded Project Authors, из google/fonts @ `6c1dbe27`, без изменений | `wuwaru/assets/fonts/` | SIL OFL 1.1, `OFL-Unbounded.txt` |
| Шрифт [Noto Sans](https://github.com/notofonts/latin-greek-cyrillic) `[wdth,wght]`, © 2022 The Noto Project Authors, из google/fonts @ `6c1dbe27`, без изменений | `wuwaru/assets/fonts/` | SIL OFL 1.1, `OFL-NotoSans.txt` |
| Python 3.12 (в собранном EXE) | среда выполнения | PSF License |

Шрифт Noto Sans 600, который упаковывается в архив для игры, — отдельный входной файл
готового перевода с закреплённым SHA-256; он тоже под SIL OFL 1.1.

## Изучено как текст, код не перенесён

- [FabianFG/CUE4Parse](https://github.com/FabianFG/CUE4Parse) @ `db808145a2910f3e9b5db3a7c87abae396109b9a`
  (Apache-2.0) — описание формата записей PAK V12 и AES-чтения индекса. Чтение архивов
  (`pak_reader.py`) написано для проекта; clean-room не заявляется.
- [4sval/FModel](https://github.com/4sval/FModel), [Herselfta/FModelCLI](https://github.com/Herselfta/FModelCLI)
  (GPL-3.0) — аудит подхода; не собирались и не используются.
- [SolicenTEAM/UEExtractor](https://github.com/SolicenTEAM/UEExtractor) (MIT),
  [RealNath/wuwa-dialogue-generator](https://github.com/RealNath/wuwa-dialogue-generator) (MIT),
  [My-Denia/WuwaTerm](https://github.com/My-Denia/WuwaTerm) (MIT, только код) — исследование.
- Тестовый вектор AES — NIST SP 800-38A, F.1.6.

## Игровые данные

Wuthering Waves, её тексты, изображения и прочие ресурсы принадлежат Kuro Games.
Название игры не означает одобрения Kuro Games; проект неофициальный.

Английские тексты берутся из публичных выгрузок
[Loping151/WWUIResourcesFull](https://github.com/Loping151/WWUIResourcesFull) и
[Arikatsu/WutheringWaves_Data](https://github.com/Arikatsu/WutheringWaves_Data)
по закреплённым ревизиям (`wuwaru/native_package.py`, `research/public-text-sources.json`).
У этих выгрузок нет явной лицензии; их публичность не даёт прав на игровой текст,
и GPL этого проекта на него не распространяется. Репозиторий не содержит игровых
архивов, баз текстов, ключей и обложек.
