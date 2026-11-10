# SELF-1 / SELF-2 / SELF-3B: поддерживаемое V12-подмножество

Это спецификация принятого профиля, не полная официальная спецификация UE/игры.
Основа: закреплённые WuWaRu/CUE4Parse источники в research/source-index.json и
прежние M4.1–M4.3. Собственный Reader, не clean-room; upstream runtime не импортируется.
AES/compression-профиль SELF-2 проверен на синтетических архивах. SELF-3A/3B
подтвердили ограниченный plain-профиль на двух реальных UniverseEditorConfig PAK,
включая base index на 348,420 entries; это не подтверждает полную V12-совместимость.

Все числа little-endian. FString: signed i32 length; положительная длина UTF-8 bytes
с NUL, отрицательная — UTF-16LE units с NUL. Лимит 4096 units, внутренний NUL запрещён.

| Footer, offset от EOF-221 | Поле |
|---|---|
| 0..15 | raw GUID; в plain только нулевой, encrypted передаётся KeyProvider |
| 16 | encrypted index flag, 0/1 |
| 17 | u32 magic 0x5A6F12E1 |
| 21 | u32 version = 12 |
| 25, 33 | u64 primary index offset, size |
| 41..60 | SHA-1 primary plaintext index, включая alignment bytes |
| 61..220 | пять 32-byte ASCII codec slots с NUL и нулевым padding |

Порядок: serialized headers+payload, primary, path-hash, directory, footer.
В payload допускаются только нулевые alignment gaps ≤2047 байт перед entry,
выровненным на 2048; overlaps/trailing data отвергаются. По умолчанию каждый индекс
≤16 MiB, entries ≤4096. Mount допускает `../../../` и безопасные подкаталоги; сохраняется как
metadata и не применяется к выходному пути.

Primary: FString mount, u32 path count, u64 seed; два descriptors (u32 present=1,
u64 offset/size, SHA-1 20 bytes), u32 encoded length, encoded bytes, u32 nonencoded=0.
Descriptors должны покрывать оставшееся пространство до footer без разрывов.
Path-hash: u32 count, пары (u64 hash, u32 encoded-byte offset), u32 pruned=0.
Хеш: FNV-1a64 по lowercase полного пути UTF-16LE, начальное
`(0xCBF29CE484222325 + seed) mod 2^64`, prime 0x100000001B3.
Directory: u32 directories, FString parent с `/`, u32 files, пары (FString basename,
u32 encoded-byte offset). `/` — виртуальный корень. `0x80000000` — подтверждённый
реальным patch sentinel pruned path: путь и его hash проверяются, entry не извлекается.
Другие ссылки должны указывать точно на начала variable encoded records;
коллизии/дубликаты/traversal/ADS/absolute/Windows reserved names/links отвергаются.
Индексный path count включает pruned paths; Reader сохраняет их отдельно от активных entries.

## Масштаб индекса

Reader сохраняет default 4,096 paths и default 16 MiB на индекс. Явные per-reader
лимиты допускают до 1,000,000 paths, 128 MiB на каждый индекс и metadata budget
512 MiB по умолчанию (жёсткий максимум настройки — 1 GiB). Перед построением таблиц
проверяется оценка metadata, включающая count, размеры индексов и число каталогов.
Смещения и размеры сверяются с длиной файла.

Формат требует сверки всех индексов с directory/hash/encoded records. Reader полностью
читает и проверяет индекс; дисковое paging не предусмотрено. `read_metadata` фильтрует
по подстроке пути, расширению и странице, возвращая только выбранные entries;
`active_entries` сообщает полный активный count. Промежуточные структуры остаются в RAM
и ограничиваются metadata budget. Это оценка расхода Python metadata, не строгий RSS cap.

## AES index profile

Три индекса зашифрованы целиком AES-256-ECB, без IV и PKCS-padding. Размер каждого
кратен 16, после разобранных полей допускается 0..15 нулевых alignment bytes.
SHA-1 считается по расшифрованным bytes вместе с alignment. Это точный synthetic
профиль; иные варианты padding/hash/mount не объявляются поддержанными.
CUE4Parse Aes.cs подтверждает ECB/no padding; соглашение padding/hash дополнительно
зафиксировано нашей тестовой спецификацией, не проверено на настоящей игре.

KeyProvider получает archive Path/raw GUID и возвращает разрешённые 32 bytes или None.
Нет получения ключа → KEY_UNAVAILABLE, отсутствует библиотека → ENCRYPTED_INDEX,
неверный ключ/повреждение → CORRUPTED_ARCHIVE (надёжно различить их нельзя).
SHA-1 + структура всех индексов + directory/path hashes + serialized headers обязательны.
Даже корректный SHA-1 произвольного plaintext не делает его допустимым индексом.
Ключ не кэшируется между операциями, не логируется и не хранится проектом.
Zeroization Python bytes не гарантируется; основание использования обеспечивает caller.

## Encoded entries и compression blocks

Первый u32 raw преобразуется в logical bits:
`(raw >> 16)&0x3f | (raw&0xffff)<<6 | (raw&0x10000000)>>6 |
(raw&0x0fc00000)<<1 | (raw&0xc0000000)>>1 | (raw&0x20000000)<<2`.
Следующий custom byte обязан быть 0. Logical поля:

| Bits | Значение |
|---|---|
| 0..5 | block size / 2048; 63 означает следующий u32 exact block size |
| 6..21 | число compression blocks |
| 22 | payload encryption; сейчас отвергается |
| 23..28 | compression slot 1..5; 0 — plain |
| 29 | stored size u32-safe |
| 30 | offset u32-safe после WuWa size-before-offset swap |
| 31 | decoded size u32-safe после swap |

Далее decoded size, offset (u32 или u64 по флагам), для compressed — stored size;
для >1 блока дополнительно u32 stored length каждого блока. Для одного длина=stored size.
Нулевой method требует нулевых block count/size. Plain `0xe0000000, custom=0,
u32 size, u32 offset` SELF-1 остаётся совместимым. Проверены all-u32 и all-u64
поля; смешанные ширины parser разбирает, но отдельными fixtures не подтверждены.

Serialized header: u64 offset=0, u64 stored size, u64 decoded size, u32 method,
SHA-1 stored payload; если method!=0 — u32 block count, пары (u64 start,end),
затем flags byte=0 и u32 block size. Plain header=53 bytes,
compressed header=57+16*count. Block offsets относительны к entry offset,
сразу за header, без gaps/padding. Все размеры и таблицы сверяются с encoded index.
ArchiveEntry.sha1 — stored digest; EntryVerification.sha1/sha256 — decoded bytes.
verify_archive дополнительно возвращает SHA-256 всех исходных bytes архива.

Zlib (zlib wrapper, wbits=15) и Gzip (wbits=31), один самостоятельный stream на блок.
Требуются точный decoded length, EOF и отсутствие trailing/unused/unconsumed bytes.
Concatenated gzip members, raw-deflate, LZ4/LZMA и другие codecs не поддержаны.
Oodle без явно предоставленного разрешённого decoder → DECOMPRESSION_UNAVAILABLE.
Неиспользуемый Oodle/unknown slot сам по себе не запрещает plain entry.

## Лимиты и доказательства

≤8192 compressed blocks суммарно, ≤2 MiB stored/block, ≤1 MiB decoded/block.
По умолчанию decoded file ≤256 MiB, total ≤512 MiB, квоты можно явно повысить.
Payload читается/пишется блоками; индексы ограничены и читаются в память.
Отмена проверяется до/после bounded decrypt и каждого decode, во время plain streaming
и перед публикацией. Выходной файл публикуется только после успешных хешей;
ошибка убирает temp, существующий output не заменяется.

| Возможность | Реально проверено |
|---|---|
| Plain V12, Unicode/empty, несколько plain entries | SELF-1 и полный regression |
| AES-256-ECB primary/hash/directory, GUID/key/error paths | Independent synthetic producer + NIST F.1.6 AES vector |
| Compressed single/multiple blocks, implicit/explicit block size, all-u32/all-u64 | Zlib/Gzip synthetic archives; новые compressed fixtures содержат одну entry |
| Decoding/memory/отмена/unsafe sizes/paths | 16 SELF-2 tests, подробная карта в SELF-2-REPORT.md |
| Большие payload | SELF-1 32 MiB plain, SELF-2 32 MiB compressed, benchmark 64 MiB |
| Архив >4 GiB | Только прежний sparse metadata test; payload verification NOT_RUN |
| Другие mount layouts/nonencoded/custom/encrypted payload, partial encryption | Не поддержаны |
| Oodle decode, IoStore/signed/иные footer layouts | Не поддержаны; Oodle callable только граница интеграции |
| Реальный UniverseEditorConfig patch: 3560 plain entries + 486 pruned; 604-byte extract | SELF-3A, один пакет; client/active-version UNKNOWN |
| Реальный UniverseEditorConfig base: 348420 plain entries, pruned=0; 2 unique small extracts | SELF-3B; index/header validation, peak tracemalloc 304150375 bytes |
| 400001-entry valid synthetic index; large hash-index corruption | SELF-3B regression test; no claim about other PAK producers |
| Реальные языковые SQLite / полная V12 / клиент / установка | 0 SQLite; UNKNOWN / NOT_RUN, installation_allowed=false |

Frozen plain abc vector SELF-1 (486 bytes) не вызывает writer в runtime теста.
SELF-2 producer не импортирует application reader/writer; AES vector взят из NIST,
Zlib abc также проверяется frozen bytes. Внешнего game/open-corpus PAK нет;
синтетическая согласованность не доказывает совместимость с иными producers.
