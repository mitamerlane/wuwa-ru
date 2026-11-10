# Build from the repository root: python -m PyInstaller packaging/WuWaRussian.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

spec_location = Path(SPECPATH).resolve()
project_root = next((candidate for candidate in (spec_location, *spec_location.parents)
                     if (candidate / 'wuwaru_desktop.py').is_file()), spec_location.parent)
hidden = collect_submodules('wuwaru')
a = Analysis(
    [str(project_root / 'wuwaru_desktop.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(project_root / 'research/public-text-sources.json'), 'wuwaru'),
           (str(project_root / 'wuwaru/assets/fonts'), 'wuwaru/assets/fonts'),
           (str(project_root / 'wuwaru/assets/icons'), 'wuwaru/assets/icons')],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
# Qt resolves ICU from Windows. Do not freeze unrelated ICU binaries discovered
# elsewhere on the build machine (for example, an incompatible Poppler runtime).
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.casefold() not in {'icuuc.dll', 'icudt78.dll'}]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WuWaRussian',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(project_root / 'packaging/icons/app.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='WuWaRussian',
)
