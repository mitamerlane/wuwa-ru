"""Russian CLI for read-only discovery and offline translation."""
import argparse
import json
import sys
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .discovery import discover, inspect_installation
from .manifest import create_manifest, write_manifest
from .extraction import extract, import_databases
from . import translation_cli
from .resource_probe import probe_resources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='WuWa Russian Manager — READ_ONLY / BUILD_OFFLINE')
    commands = parser.add_subparsers(dest='command', required=True)
    translation_cli.register(commands)
    find = commands.add_parser('discover', help='Найти установки без запуска игры')
    find.add_argument('--game', type=Path, action='append', default=[], help='Папка игры')
    find.add_argument('--steam', type=Path, action='append', default=[], help='Папка Steam')
    find.add_argument('--no-system', action='store_true', help='Не читать реестр Windows')
    scan = commands.add_parser('scan', help='Создать манифест вне игры')
    scan.add_argument('--game', type=Path, required=True, help='Папка игры')
    scan.add_argument('--output', type=Path, required=True, help='Новый JSON вне игры')
    scan.add_argument('--resource-version', help='Явный выбор каталога ресурсов')
    scan.add_argument('--hash-packages', action='store_true', help='Читать все PAK для SHA-256 (долго)')
    probe = commands.add_parser('probe-resources', help='Диагностика PAK footer/языков/версий без дешифрования')
    probe.add_argument('--game', type=Path, required=True)
    probe.add_argument('--output', type=Path, required=True, help='Новый JSON вне игры')
    probe.add_argument('--resource-version')
    probe.add_argument('--hash-language-packages', action='store_true',
                       help='Дважды прочитать MountLang PAK для полной SHA-256 привязки')
    for command in ('extract', 'import-db'):
        sub = commands.add_parser(command, help='Проверить английские SQLite и создать новый staging')
        sub.add_argument('--game', type=Path, required=True)
        sub.add_argument('--output', type=Path, required=True, help='Новый каталог вне игры')
        sub.add_argument('--package', action='append', required=True,
                         help='Относительный путь PAK из игрового манифеста; можно повторять')
        sub.add_argument('--resource-version')
        sub.add_argument('--expected-inventory', help='Ожидаемый fingerprint scan (защита от устаревания)')
        if command == 'import-db':
            sub.add_argument('--source', type=Path, required=True, help='Каталог извлечённых SQLite')
    args = parser.parse_args(argv)
    try:
        if args.command == 'translation':
            print(json.dumps({'schema_version': 1, **translation_cli.execute(args)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'discover':
            result = discover(explicit=args.game, steam_roots=args.steam, system=not args.no_system)
            print(json.dumps(asdict(result), default=str, ensure_ascii=False, indent=2))
            return 0 if result.installations else 1
        item = inspect_installation(args.game)
        # Use identical discovery evidence for scan and extraction fingerprints.
        if item.root.parent.name.casefold() == 'common':
            found = discover(steam_roots=[item.root.parent.parent.parent], system=False)
            for candidate in found.installations:
                if candidate.root == item.root:
                    item = candidate
        if args.command == 'probe-resources':
            report = probe_resources(item, resource_version=args.resource_version,
                                     hash_language_packages=args.hash_language_packages)
            write_manifest(report, args.output)
            print(json.dumps({'report': str(args.output.resolve()),
                              'packages': len(report['packages']),
                              'client_version': report['client_version'],
                              'compatibility': report['compatibility']}, ensure_ascii=False, indent=2))
            return 0
        if args.command in ('extract', 'import-db'):
            options = dict(packages=args.package, resource_version=args.resource_version,
                           expected_inventory=args.expected_inventory)
            if args.command == 'extract':
                report = extract(item, args.output, **options)
            else:
                report = import_databases(item, args.source, args.output, **options)
            print(json.dumps({'staging': str(args.output.resolve()),
                              'databases': len(report['databases']),
                              'client_version': report['client_version'],
                              'source_binding': report['source_binding'],
                              'compatibility': 'UNKNOWN'}, ensure_ascii=False, indent=2))
            return 0
        manifest = create_manifest(item, resource_version=args.resource_version,
                                   hash_packages=args.hash_packages)
        write_manifest(manifest, args.output)
        print(json.dumps({'report': str(args.output.resolve()), 'mode': 'READ_ONLY',
                          'packages': len(manifest['packages']),
                          'resource_version': manifest['resource_version'],
                          'compatibility': manifest['compatibility'],
                          'warnings': manifest['warnings']}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Операция отменена.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
