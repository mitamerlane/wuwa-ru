"""PyInstaller entry point of the installer."""
import sys

from installer.ui import run

if __name__ == '__main__':
    raise SystemExit(run(sys.argv))
