# PyInstaller spec - run on Windows:  pyinstaller installer/teloude.spec --noconfirm
# One-dir build (fast start, fewer antivirus false positives than one-file).
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821
ICON = str(ROOT / "assets" / "icon.ico")
ASSETS = str(ROOT / "assets")

# Optional credentials injected by scripts/inject_credentials.py. They are bundled at the *root* of
# the archive, which is where app.infrastructure.build_config looks for them (sys._MEIPASS).
datas = [(ASSETS, "assets")]
_creds = ROOT / "app" / "teloude_api.json"
if _creds.exists():
    datas.append((str(_creds), "."))

a = Analysis(  # noqa: F821
    [str(ROOT / "app" / "__main__.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["telethon.network.connection.tcpmtproxy", "PIL._tkinter_finder"],
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Teloude",
    console=False,
    icon=ICON,  # embedded into Teloude.exe
    version=str(ROOT / "installer" / "version_info.txt"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Teloude", upx=False)  # noqa: F821
