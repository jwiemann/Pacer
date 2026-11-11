# PyInstaller spec: one folder (dist/Pacer/Pacer.exe), no console window.
# Built by build.ps1; the sources are the flat modules one directory up.
import os

from PyInstaller.utils.hooks import copy_metadata

HERE = os.path.dirname(os.path.abspath(SPEC))
SRC = os.path.dirname(HERE)

a = Analysis(
    [os.path.join(SRC, "pacer.py")],
    pathex=[SRC],
    # the roles pacer.py starts are imported lazily inside functions: name them so nothing is missed
    hiddenimports=["tray", "dashboard", "governor", "watchdog", "updater", "copilot_monitor", "openrouter_monitor", "autostart", "pystray._win32"],
    # scripts that run inside WSL / as a scheduled task: shipped as plain files, copied to the data dir at start
    # fonts/: the bundled JetBrains Mono, loaded by ui_widgets.init_fonts() from <_MEIPASS>/fonts
    # pg8000 and scramp read their own version from the installed package metadata at import: ship that metadata too
    datas=[(os.path.join(SRC, f), ".") for f in ("watchdog-inner.cjs", "repair-wsl.ps1")] + [(os.path.join(SRC, "fonts"), "fonts")]
          + copy_metadata("pg8000") + copy_metadata("scramp"),
    excludes=["unittest", "pydoc", "test", "numpy", "matplotlib", "scipy", "pandas"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Pacer", console=False,
          icon=os.path.join(HERE, "pacer.ico"), contents_directory="_internal")
coll = COLLECT(exe, a.binaries, a.datas, name="Pacer")
