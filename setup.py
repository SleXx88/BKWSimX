# setup.py
import sys
import os
from cx_Freeze import setup, Executable
from src.bkwsimx import __version__

# ---------------------------------------------------------------------------
# Build-Optionen für cx_Freeze
# ---------------------------------------------------------------------------
build_exe_options = {
    "include_path": [os.path.join(os.getcwd(), "src")],
    "packages": ["gui", "logic", "worker"],
    "includes": [
        "gui.mainwindow",
        "gui.ui_main_window",
        "gui.widgets",
        "logic.calculation",
        "worker.calcworker",
        "bkwsimx.version",
    ],
    "excludes": [
        "tkinter", "PyQt4", "PyQt5", "PySide2", "PySide6",
        "IPython", "pytest", "sphinx", "Cython", "numba",
        "cupy", "dask", "torch", "jax", "sympy", "statsmodels",
        "tables", "boto3", "botocore",
        "gi", "cairo", "cairocffi", "wx",
        "openpyxl", "lxml", "bs4", "docutils", "yaml",
        "pandas.tests",
        "matplotlib.tests",
        "matplotlib.sphinxext",
    ],
    "include_files": [
        ("src/resources/batteries.json",    "lib/resources/batteries.json"),
        ("src/resources/inverters.json",    "lib/resources/inverters.json"),
        ("src/resources/pv_systems.json",   "lib/resources/pv_systems.json"),
        ("src/ui/main_window.ui",           "lib/ui/main_window.ui"),
        ("src/ui/pv_generator_page.ui",     "lib/ui/pv_generator_page.ui"),
        ("src/icons/icon.ico",              "lib/icons/icon.ico"),
        ("src/icons/splash.png",            "lib/icons/splash.png"),
    ],
    "include_msvcr": True,
}

# ---------------------------------------------------------------------------
# Executables definieren (mit korrekten Shortcut-Parametern)
# ---------------------------------------------------------------------------
base = "Win32GUI" if sys.platform == "win32" else None

executables = [
    Executable(
        script="src/main.py",
        base=base,
        target_name="BKWSimX.exe",
        icon="src/icons/icon.ico",
        shortcut_name="BKWSimX",            # Startmenü-Shortcut 
        shortcut_dir="ProgramMenuFolder",    # 
    ),
    Executable(
        script="src/launch_debug.py",
        base=None,
        target_name="BKWSimX_debug.exe",
        icon="src/icons/icon.ico",
        shortcut_name="BKWSimX Debug",
        shortcut_dir="ProgramMenuFolder",
    ),
]

# ---------------------------------------------------------------------------
# MSI-Optionen: Upgrade, Pfad, Lizenz & Shortcuts über data
# ---------------------------------------------------------------------------
bdist_msi_options = {
    "all_users": True,  # Installation für alle Benutzer :contentReference[oaicite:2]{index=2}
    "add_to_path": False,  # nicht automatisch in PATH :contentReference[oaicite:3]{index=3}
    # 64-Bit-Standardpfad – für 32-Bit käme [ProgramFilesFolder]
    "initial_target_dir": r"[ProgramFiles64Folder]\BKWSimX",  # :contentReference[oaicite:4]{index=4}
    # Upgrade-Code (gleich lassen für sauberen Versionswechsel)
    "upgrade_code": "{12345678-90AB-CDEF-1234-567890ABCDEF}",  # :contentReference[oaicite:5]{index=5}
    # Lizenz-Dialog (RTF)
    "license_file": os.path.join("src", "LICENSE.rtf"),      # :contentReference[oaicite:6]{index=6}
    # MSI-Tabelle „Shortcut“: Desktop-Verknüpfung
    "data": {
        "Shortcut": [
            # (Id, Directory_, Name, Component_, Target, Arguments, Description, Hotkey, Icon, IconIndex, ShowCmd, WkDir)
            (
                "DesktopShortcut",
                "DesktopFolder",
                "BKWSimX",
                "TARGETDIR",
                "[TARGETDIR]BKWSimX.exe",
                None,
                "Starte BKWSimX",
                None, None, None, None, None
            ),
        ],
    },
}

# ---------------------------------------------------------------------------
# Setup-Aufruf
# ---------------------------------------------------------------------------
setup(
    name="BKWSimX",
    version=__version__,
    description="Simulation & Planung steckerfertiger PV-Anlagen",
    options={
        "build_exe": build_exe_options,
        "bdist_msi": bdist_msi_options,
    },
    executables=executables,
)
