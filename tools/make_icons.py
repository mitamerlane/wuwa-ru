"""Render packaging/icons/*.svg into multi-size .ico files: python tools/make_icons.py [--preview DIR]

Needs only PySide6 (QtSvg). Each ICO entry is a PNG (Vista+ format), sizes 16…256.
The app icon is also copied to wuwaru/assets/icons/app.ico for the window icon.
"""
from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / 'packaging' / 'icons'
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_png(svg: Path, size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise SystemExit(f'{svg}: SVG не читается')
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(data)


def ico_bytes(pngs: dict[int, bytes]) -> bytes:
    header = struct.pack('<HHH', 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, blobs = b'', b''
    for size, png in sorted(pngs.items()):
        side = 0 if size >= 256 else size
        entries += struct.pack('<BBBBHHII', side, side, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    return header + entries + blobs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--preview', help='also write <name>-<size>.png previews here')
    args = ap.parse_args()
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841 - QPainter needs it
    for svg in sorted(ICONS.glob('*.svg')):
        pngs = {size: render_png(svg, size) for size in SIZES}
        out = svg.with_suffix('.ico')
        out.write_bytes(ico_bytes(pngs))
        print(f'{out.relative_to(ROOT)} ({out.stat().st_size} байт)')
        if args.preview:
            folder = Path(args.preview)
            folder.mkdir(parents=True, exist_ok=True)
            for size in (16, 32, 48, 256):
                (folder / f'{svg.stem}-{size}.png').write_bytes(pngs[size])
    target = ROOT / 'wuwaru' / 'assets' / 'icons'
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ICONS / 'app.ico', target / 'app.ico')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
