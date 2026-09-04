# PyInstaller spec for starstack.exe -- one file, no console, owl icon.
#   pyinstaller starstack.spec
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None
here = os.path.abspath(".")

datas = [
    (os.path.join(here, "starstack.py"), "."),            # the button reads it for its own path logic
    (os.path.join(here, "docs", "faces", "*.png"), os.path.join("docs", "faces")),
    (os.path.join(here, "docs", "owl_256.png"), "docs"),
    (os.path.join(here, "docs", "icon_256.png"), "docs"),
    (os.path.join(here, "docs", "starstack.ico"), "docs"),
]
datas += collect_data_files("astropy", include_py_files=False)
datas += collect_data_files("skimage", include_py_files=False)

hidden = (collect_submodules("astroalign") + collect_submodules("sep_pjw") + collect_submodules("sep")
          + collect_submodules("skimage.transform") + collect_submodules("scipy.ndimage")
          + collect_submodules("scipy.spatial") + ["imagecodecs", "tifffile", "PIL.ImageTk"])

a = Analysis(
    ["starstack_app.py"],
    pathex=[here],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "IPython", "pytest", "tkinter.test", "astropy.tests"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="starstack",
    icon=os.path.join(here, "docs", "starstack.ico"),
    console=False,                 # no black console window behind the button
    debug=False,
    strip=False,
    upx=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
)
