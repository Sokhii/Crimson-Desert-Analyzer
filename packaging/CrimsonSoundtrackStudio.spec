# PyInstaller spec - portable one-folder Windows build.
# Build from the repository root:  pyinstaller packaging/CrimsonSoundtrackStudio.spec --noconfirm
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"

datas = [
    (str(SRC / "cstudio" / "resources"), "cstudio/resources"),
    (str(ROOT / "docs" / "research"), "cstudio/resources/research"),
]

a = Analysis(
    [str(ROOT / "CrimsonSoundtrackStudio.py")],
    pathex=[str(SRC)],
    datas=datas,
    hiddenimports=["lz4.block", "cryptography.hazmat.primitives.ciphers"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtPdf", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CrimsonSoundtrackStudio",
    console=False,
    icon=None,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="CrimsonSoundtrackStudio")
