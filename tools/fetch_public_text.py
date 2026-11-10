"""Download hash-pinned public JSON only. Never execute any downloaded content."""
import argparse
import hashlib
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wuwaru.manifest import fingerprint
from wuwaru.paths import inside
from wuwaru.public_corpus import read_lock
from wuwaru.translation_db import offline_path


def fetch(lock, source_name, destination):
    source = read_lock(lock, source_name)
    destination = offline_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for file in source['files']:
        target = inside(destination, file['path'])
        if target.exists():
            if fingerprint(target, True, None)['sha256'] != file['sha256']:
                raise ValueError('Существующий JSON не совпадает с lock: ' + file['path'])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        url = ('https://raw.githubusercontent.com/' + source['repository'].removeprefix('https://github.com/') +
               '/' + source['revision'] + '/' + urllib.parse.quote(file['path'], safe='/'))
        with tempfile.TemporaryDirectory(dir=target.parent, prefix='.fetch-') as temporary:
            staged = Path(temporary) / 'data.json'
            digest, size = hashlib.sha256(), 0
            with urllib.request.urlopen(url, timeout=90) as response, staged.open('xb') as output:
                while block := response.read(65536):
                    size += len(block)
                    if size > file['size']:
                        raise ValueError('Размер загрузки превысил lock')
                    digest.update(block)
                    output.write(block)
            if size != file['size'] or digest.hexdigest() != file['sha256']:
                raise ValueError('SHA-256/размер загрузки не совпал с lock')
            offline_path(target)
            os.rename(staged, target)
    return len(source['files'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path('research/public-text-sources.json'))
    parser.add_argument('--source-name', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print('Verified JSON files:', fetch(args.lock, args.source_name, args.destination))
