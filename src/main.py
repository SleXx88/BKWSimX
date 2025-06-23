# -*- coding: utf-8 -*-
"""BKWSimX – Programmstart
• Leitet Logs in Datei **und** Debug‑Konsole um.
• Schreibt bkwsimx.log im Benutzerprofil (nicht im Programmverzeichnis).
• Fängt ungefangene Exceptions ab und zeigt einen Fehlerdialog.
"""

from __future__ import annotations
from bkwsimx import __version__

# ---------------------------------------------------------------------------#
# 0) Environment‑Setup *vor* allen Qt‑Imports                                #
# ---------------------------------------------------------------------------#
import os
import sys
from pathlib import Path

# Per‑Monitor‑V2 DPI‑Awareness über Plattform‑Argument (kein Code‑Hack nötig)
# Per‑Monitor‑V2 aktivieren, Dark‑/Light‑Mode dem System überlassen
# Immer Light‑Mode + Per‑Monitor‑V2 DPI
# os.environ["QT_QPA_PLATFORM"] = "windows:darkmode=0,dpiawareness=3"
#os.environ["QT_QPA_PLATFORM"] = "windows:dpiawareness=3"
# Wenn du lieber qt.conf nutzt, kannst du die nächste Zeile einkommentieren.
# os.environ["QT_CONF_PATH"] = str(Path(__file__).with_name("qt.conf"))

# ---------------------------------------------------------------------------#
# Standard‑Imports *nach* den env‑Variablen                                  #
# ---------------------------------------------------------------------------#
from PyQt6 import QtCore, QtGui, QtWidgets
import builtins
import logging
import shutil
import traceback

# ---------------------------------------------------------------------------#
# Verzeichnisse & Logging                                                    #
# ---------------------------------------------------------------------------#
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

USER_DIR = Path(os.getenv("APPDATA", APP_DIR)) / "BKWSimX"
USER_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = USER_DIR / "bkwsimx.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)
logger.info("Logging initialisiert – Log‑Datei: %s", LOG_FILE)
logging.getLogger().setLevel(logging.INFO)

# ---------------------------------------------------------------------------#
# Import‑Tracking (optional)                                                 #
# ---------------------------------------------------------------------------#
_original_import = builtins.__import__
imported_modules: set[str] = set()

def _tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    imported_modules.add(name)
    return _original_import(name, globals, locals, fromlist, level)

builtins.__import__ = _tracking_import  # type: ignore

# ---------------------------------------------------------------------------#
# GPU‑Workaround für Qt WebEngine                                            #
# ---------------------------------------------------------------------------#
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    "--disable-gpu --disable-gpu-compositing --disable-software-rasterizer "
    "--disable-webgl --log-level=3"
)

# ---------------------------------------------------------------------------#
# src/ in sys.path eintragen                                                 #
# ---------------------------------------------------------------------------#
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# ---------------------------------------------------------------------------#
# Globaler Exception‑Hook                                                    #
# ---------------------------------------------------------------------------#

def _excepthook(exc_type, exc_value, exc_tb):
    logger.critical("Ungefangene Exception", exc_info=(exc_type, exc_value, exc_tb))
    app = QtWidgets.QApplication.instance()
    if app:
        msg = QtWidgets.QMessageBox()
        msg.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        msg.setWindowTitle("Unerwarteter Fehler")
        msg.setText(f"{exc_type.__name__}: {exc_value}")
        msg.setDetailedText("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        msg.exec()

sys.excepthook = _excepthook

# ---------------------------------------------------------------------------#
# Ressourcen‑Pfad für cx_Freeze                                              #
# ---------------------------------------------------------------------------#

def _resource_path(rel: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", APP_DIR))
    for cand in (base / rel, base / "lib" / rel):
        if cand.exists():
            return str(cand)
    raise FileNotFoundError(rel)

# ---------------------------------------------------------------------------#
# User‑Profil & Default‑Konfiguration                                        #
# ---------------------------------------------------------------------------#

def setup_user_profile() -> None:
    """Erstellt Konfig‑ und Datenfiles im USER_DIR, falls nicht vorhanden."""
    resource_names = [
        "batteries.json",
        "inverters.json",
        "pv_systems.json",
        "config_default.json",
    ]
    base_src = APP_DIR / ("lib/resources" if getattr(sys, "frozen", False) else "resources")
    for name in resource_names:
        src = base_src / name
        dst = USER_DIR / name.replace("config_default", "config")
        if src.exists() and not dst.exists():
            shutil.copy(src, dst)

setup_user_profile()

# ---------------------------------------------------------------------------#
# GUI‑Hauptfenster importieren                                              #
# ---------------------------------------------------------------------------#
try:
    from gui.mainwindow import MainWindow  # type: ignore
except ModuleNotFoundError as exc:
    logger.critical("GUI‑Modul fehlt: %s", exc, exc_info=True)
    raise SystemExit(f"Fehler beim Laden der GUI: {exc.name}\nOriginal: {exc}")

# ---------------------------------------------------------------------------#
# main() – Einstiegspunkt                                                   #
# ---------------------------------------------------------------------------#

def main() -> None:
    from PyQt6.QtWidgets import QStyleFactory

    logger.info(f"BKWSimX {__version__} startet …")
    app = QtWidgets.QApplication(sys.argv)

    # Debug‑Ausgabe des aktuellen DPI‑Modus (entfernbar)
    if sys.platform == "win32":
        import ctypes
        awareness = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(awareness))
        logger.debug("DPI‑Awareness (0=UA,1=SA,2=PM,3=PMv2): %s", awareness.value)
        logger.debug("Qt logical DPI: %s", app.primaryScreen().logicalDotsPerInch())

    # Splash‑Screen
    pix = QtGui.QPixmap(_resource_path("icons/splash.png")).scaled(
        400, 400, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    splash = QtWidgets.QSplashScreen(pix, QtCore.Qt.WindowType.SplashScreen | QtCore.Qt.WindowType.WindowStaysOnTopHint)
    splash.show()
    app.processEvents()

    app.setApplicationName("BKWSimX")
    app.setOrganizationName("Martin Teske")
    app.setStyle(QStyleFactory.create("WindowsVista"))

    window = MainWindow()
    splash.finish(window)
    window.show()

    exit_code = app.exec()
    logger.info("BKWSimX beendet mit Exit‑Code %s", exit_code)
    sys.exit(exit_code)

# ---------------------------------------------------------------------------#
# Entry‑Point                                                               #
# ---------------------------------------------------------------------------#
if __name__ == "__main__":
    main()
