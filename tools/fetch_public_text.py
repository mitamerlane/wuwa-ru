"""Download hash-pinned public JSON only. Never execute any downloaded content."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wuwaru.public_fetch import fetch  # noqa: E402,F401


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path('research/public-text-sources.json'))
    parser.add_argument('--source-name', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print('Verified JSON files:', fetch(args.lock, args.source_name, args.destination))
