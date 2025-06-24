from __future__ import annotations
# """gui/mainwindow.py

# Qt‑GUI‑Klasse für BKWSimX.

# * Lädt die Designer‑Datei `ui/main_window.ui` zur Laufzeit ⇒ kein pyuic‑Schritt nötig.
# * Verbindet den *Berechnen‑Button* mit einem `CalcWorker` (QThread).
# * Liest beim Start **alle** Eingaben aus den Widgets und füllt ein `Settings`‑Dataclass,
#   das an `run_calculation()` geht.
# * Ergebnis‑Slot erhält das Dict und aktualisiert GUI‑Elemente (TODO‑Marker).

# **Hinweis zu `from __future__ import annotations`**  – muss laut PEP 563 ganz
# oben stehen. Daher ist die Import‑Zeile jetzt an erster Stelle, vor dem
# Dokstring, um den SyntaxError zu vermeiden.

# Copyright © 2025 Martin Teske – MIT‑Lizenz
# """
from bkwsimx import __version__

import logging
import calendar
import mplcursors
import json
import sys

from dataclasses import asdict
from pathlib                            import Path
from PyQt6                              import QtCore, QtWidgets, uic
from PyQt6.QtGui        import QStandardItemModel, QStandardItem, QFont, QIcon, QFontDatabase
from PyQt6.QtWidgets    import QHeaderView, QMessageBox, QFileDialog, QVBoxLayout, QPlainTextEdit, QDialog, QPushButton, QProgressDialog
from PyQt6.QtWebEngineWidgets           import QWebEngineView
from PyQt6.QtWebChannel                 import QWebChannel
from PyQt6.QtCore                       import QUrl, QObject, pyqtSignal, pyqtSlot
from matplotlib.backends.backend_qtagg  import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure                  import Figure

import logic.calculation as calc_mod
from datetime import datetime

from gui.widgets import TiltWidget, AzimuthWidget

from logic.calculation import (
    GeneratorConfig, Settings, run_calculation,
    _pv_systems, _inverters, _batteries,
    _sys_by_name, _inv_by_id, _batt_by_id, _batt_by_model, _inv_by_model, get_battery_spec,
    calculate_avg_system_efficiency, calculate_avg_storage_efficiency, compute_total_losses,
)
from worker.calcworker import CalcWorker
import requests                           # ➊ für Geocoding

HEADERS = {"User-Agent": "BKWSimX/1.0"}   # ➋ Nominatim verlangt UA

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _month_abbr(idx: int | str) -> str:
    """Gibt die englische Monatsabkürzung zurück, 1→'Jan'. Akzeptiert auch String-Indices."""
    # Key aus JSON ist ein String, also sicherheitshalber in int konvertieren
    idx_int = int(idx)
    return calendar.month_abbr[idx_int]

def _consecutive_month_ranges(month_list: list[int]) -> list[tuple[int, int]]:
    """
    z.B. [11,12,1,2]  ➜  [(11, 2)]
         [1,2,3,7,8] ➜  [(1,3), (7,8)]
    """
    if not month_list:
        return []

    months = sorted(set(month_list))
    ranges: list[tuple[int, int]] = []
    start = prev = months[0]

    for m in months[1:]:
        if (prev % 12) + 1 != m:           # Lücke
            ranges.append((start, prev))
            start = m
        prev = m
    ranges.append((start, prev))

    # Bei überlappenden Bereichen über den Jahreswechsel zusammenfassen
    if len(ranges) > 1 and ranges[0][0] == 1 and ranges[-1][1] == 12:
        wrap_start = ranges[-1][0]
        wrap_end = ranges[0][1]
        ranges = ranges[1:-1]
        ranges.insert(0, (wrap_start, wrap_end))

    return ranges

def build_mppt_fields(n: int) -> None:
    # TODO: echte Felder dynamisch erzeugen.
    # Vorläufig nur Platzhalter, damit der Code läuft.
    print(f"[DEBUG] MPPT‑Felder für {n} Tracker würden hier gebaut.")
    
# Deutsche Monatsnamen selbst hinterlegen (keine Locale-Spielchen nötig)
_MON_DE = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

def _disabled_label(disabled: list[int]) -> str:
    """
    Liefert z. B.
      »Mögliche Speicherabschaltung: Dezember bis Januar, März«
    """
    if not disabled:
        return "nicht notwendig"

    ranges = _consecutive_month_ranges(disabled)

    parts: list[str] = []
    for start, end in ranges:
        # Prüfen, ob die Range über den Jahreswechsel “wrappt”
        if start > end: # z. B. 11, 12, 1, 2
            parts.append(f"{_MON_DE[end]} bis {_MON_DE[start]}")
        elif start == end:  # z. B. 1, 2, 3
            parts.append(_MON_DE[start])
        else:   # z. B. 3, 4, 5, 6
            parts.append(f"{_MON_DE[start]} bis {_MON_DE[end]}")

    #return "Mögliche Speicherabschaltung: " + ", ".join(parts[::-1])
    return " und ".join(parts[::-1])

def _resource_path(rel: str) -> str:
    """
    Liefert einen plattform-unabhängigen Pfad, der sowohl im
    Quell-Code-Verzeichnis als auch in der gefrorenen EXE funktioniert.
    Sucht zuerst im Basis­verzeichnis, dann in »lib/« (cx_Freeze).
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for cand in (base / rel, base / "lib" / rel):
        if cand.exists():
            return str(cand)
    raise FileNotFoundError(rel)

# ---------------------------------------------------------------------------
# MapBridge
# ---------------------------------------------------------------------------
class MapBridge(QObject):
    """Brücke zwischen JavaScript und PyQt für Klick-Koordinaten."""
    coordinatesChanged = pyqtSignal(float, float)

    @pyqtSlot(float, float)
    def sendCoordinates(self, lat: float, lon: float) -> None:
        # Wird aus JS aufgerufen
        self.coordinatesChanged.emit(lat, lon)

# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    """Haupt‑Fenster.  Lädt .ui zur Laufzeit – kein Kompilieren nötig."""

    def __init__(self) -> None:
        super().__init__()
        
        # Fenster- & App-Icon setzen  → erscheint in Titelleiste + About-Dialog
        icon = QIcon(_resource_path("icons/icon.ico"))
        self.setWindowIcon(icon)                    # Titelleisten-Icon
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)                 # Icon für QMessageBox.about()
            
        self._has_result = False

        # ------------------------------------------------------------
        #   1) UI laden
        # ------------------------------------------------------------
        ui_path = (
            Path(__file__).resolve().parent.parent / "ui" / "main_window.ui"
        )
        if not ui_path.exists():
            raise FileNotFoundError(ui_path)
        uic.loadUi(ui_path, self)                        # type: ignore[arg-type]
        
        # ------------------------------------------------------------
        #  eigener Fenstertitel (überschreibt Wert aus der .ui-Datei)
        # ------------------------------------------------------------
        self.setWindowTitle(f"BKWSimX {__version__} – Simulation & Planung steckerfertiger PV-Anlagen")

        # ------------------------------------------------------------
        #   Debug-Fenster initialisieren (versteckt)
        # ------------------------------------------------------------
        calc_mod.CURRENT_SCENARIO = None  # wird von CalcWorker gesetzt

        self._debug_window = QDialog(self)
        self._debug_window.setWindowTitle("Debug-Konsole")
        # Layout und Widgets
        layout = QVBoxLayout(self._debug_window)
        self._debug_console = QPlainTextEdit(self._debug_window)
        self._debug_console.setReadOnly(True)
        layout.addWidget(self._debug_console)
        # 1) System-Monospace-Font holen
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        # 2) Alternativ, falls das nicht liefert, hart auf Courier umschwenken:
        if not fixed.fixedPitch():  
            fixed = QFont("Courier New")
            fixed.setStyleHint(QFont.StyleHint.Monospace)
        # 3) Größe anpassen (optional)
        fixed.setPointSize(10)
        # 4) Anwenden
        self._debug_console.setFont(fixed)
        # Clear-Button
        self._btn_clear_debug = QPushButton("Löschen", self._debug_window)
        layout.addWidget(self._btn_clear_debug)
        self._btn_clear_debug.clicked.connect(self._debug_console.clear)
        self._debug_window.resize(600, 300)
        self._debug_window.hide()

        # Logging-Handler, der in das Text-Widget schreibt, ohne Modul-Name:
        class QtHandler(logging.Handler):
            """Logging-Handler, der Einträge in das QPlainTextEdit schreibt, mit SCN-Präfix und Zeitstempel."""
            def __init__(self, widget: QPlainTextEdit):
                super().__init__()
                self.widget = widget

            def emit(self, record: logging.LogRecord) -> None:
                # 1) Zeitstempel erzeugen
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]
                # 2) Message inkl. Argumente (record.getMessage() macht das %-Formatting)
                text = record.getMessage()
                # 3) Szenario-Präfix voranstellen (wenn gesetzt)
                scn = getattr(calc_mod, 'CURRENT_SCENARIO', None)
                if scn is not None:
                    text = f"[SCN{scn}] {text}"
                # 4) Gesamte Zeile zusammenbauen
                full = f"{ts} [{record.levelname}] {text}"
                # 5) Thread-sicher in die GUI-Schleife einreihen
                QtCore.QMetaObject.invokeMethod(
                    self.widget,
                    "appendPlainText",
                    QtCore.Qt.ConnectionType.QueuedConnection,
                    QtCore.Q_ARG(str, full)
                )

        # direkt nach der Handler-Definition
        self._qt_handler = QtHandler(self._debug_console)
        self._qt_handler.setLevel(logging.DEBUG)
        # Wir verwenden jetzt nur noch Formatter für Level/etc., das eigentliche msg bauen wir selbst:
        fmt = logging.Formatter("%(message)s")
        self._qt_handler.setFormatter(fmt)

        calc_logger = logging.getLogger("logic.calculation")
        calc_logger.setLevel(logging.DEBUG)
        calc_logger.addHandler(self._qt_handler)

        # Checkbox verbinden (öffen/​schließen)
        self.checkBox_debug_window.toggled.connect(self._toggle_debug_window)

        # Wenn das Dialog-X gedrückt wird: Haken raus und nur verstecken
        def _on_debug_close(event):
            self._debug_window.hide()
            self.checkBox_debug_window.setChecked(False)
            event.ignore()

        self._debug_window.closeEvent = _on_debug_close

        # ------------------------------------------------------------
        #   1b) GUI-Elemente initialisieren
        # ------------------------------------------------------------

        # Liste aller GeneratorConfig-Objekte (wird in _update_system_overview und im Ergebnis-Tab verwendet)
        self._generator_configs: list[GeneratorConfig] = []
        
        # ────────────────────────────────────────────────────────────────
        # Karte (Leaflet + QWebChannel) in frame_Standort_Map
        # ────────────────────────────────────────────────────────────────
        # … innerhalb __init__ nach uic.loadUi(ui_path, self) …
        self.map_view = QWebEngineView(self.frame_Standort_Map)
        if self.frame_Standort_Map.layout():
            self.frame_Standort_Map.layout().addWidget(self.map_view)
        else:
            self.map_view.setGeometry(self.frame_Standort_Map.rect())
            self.map_view.setParent(self.frame_Standort_Map)

        # Bridge vorbereiten (aber noch nicht an den Channel hängen)
        self._map_bridge = MapBridge()
        self._map_bridge.coordinatesChanged.connect(self._on_map_clicked)

        # Sobald die Seite fertig geladen ist, Channel registrieren und JS initialisieren
        def _on_load_finished(ok: bool) -> None:
            if not ok:
                return
            channel = QWebChannel(self.map_view.page())
            channel.registerObject("bridge", self._map_bridge)
            self.map_view.page().setWebChannel(channel)
            # JavaScript-Call zum Erzeugen des Channel-Objekts im Browser
            init_js = """
            new QWebChannel(qt.webChannelTransport, function(ch) {
                window.bridge = ch.objects.bridge;
            });
            """
            self.map_view.page().runJavaScript(init_js)

        self.map_view.page().loadFinished.connect(_on_load_finished)

        # HTML der Leaflet-Karte (inkl. Klick-Handler & Layer-Control)
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8" />
            <style>
            html,
            body,
            #map {
                height: 100%;
                margin: 0;
                padding: 0;
            }
            </style>
            <link
            rel="stylesheet"
            href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
            />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <!-- qwebchannel.js aus Qt-Resource -->
            <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
        </head>
        <body>
            <div id="map"></div>
            <script>
            var map = L.map("map").setView([51.1657, 10.4515], 6);
            var osm = L.tileLayer(
                "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                {
                attribution: "© OpenStreetMap",
                }
            );
            osm.addTo(map);
            var marker = L.marker([51.1657, 10.4515]).addTo(map);

            map.on("click", function (e) {
                marker.setLatLng(e.latlng);
                window.bridge.sendCoordinates(e.latlng.lat, e.latlng.lng);
            });
            </script>
        </body>
        </html>
        """
        self.map_view.setHtml(html, QUrl("qrc:///"))

        # initial zentrieren erst, wenn die Seite fertig geladen ist
        self.map_view.page().loadFinished.connect(lambda ok: ok and self._update_map())

        # ------------------------------------------------------------
        #   2) Menü-Einträge verbinden
        # ------------------------------------------------------------
        self.action_ber_BKWSimX.triggered.connect(self._show_about)
        self.actionRechtliches.triggered.connect(self._show_legal)
        
        self.action_Project_open.triggered.connect(self._open_project)
        self.action_Project_save.triggered.connect(self._save_project)
        self.action_Project_save_as.triggered.connect(self._save_project_as)
        # Pfad der zuletzt gespeicherten Projektdatei
        self._current_project_path: str | None = None

        # ------------------------------------------------------------
        #   3) Ergebnis-Tab vorbereiten
        # ------------------------------------------------------------
        self._init_results_tab()

        # ------------------------------------------------------------
        #   4) „Berechnen“-Button finden & verdrahten
        # ------------------------------------------------------------
        for obj_name in ("pushButton_Berechnen", "btn_calc"):
            btn = self.findChild(QtWidgets.QPushButton, obj_name)
            if btn is not None:
                btn.clicked.connect(self._start_calc)
                self._btn_calc = btn          # für Enabled-Toggle merken
                break
        else:
            raise RuntimeError("Berechnen-Button nicht gefunden!")

        self._worker: CalcWorker | None = None

        # ------------------------------------------------------------
        #   5) Ausgabefelder (Kosten) schreibgeschützt setzen
        # ------------------------------------------------------------
        for obj_name in [
            "doubleSpinBox_Anzeige_Hardwarekosten",
            "doubleSpinBox_Anzeige_Installationskosten",
            "doubleSpinBox_Anzeige_Foerderungen",
            "doubleSpinBox_Anzeige_Gesamt",
        ]:
            w = self.findChild(QtWidgets.QDoubleSpinBox, obj_name)
            if w:
                w.setReadOnly(True)
                w.setButtonSymbols(
                    QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons
                )

        # ------------------------------------------------------------
        #   6) Hardware-Comboboxen befüllen & Signale
        # ------------------------------------------------------------
        # --- NEU: Hersteller ---------------------------------------------------
        man_names = sorted({s["manufacturer"] for s in _pv_systems if s.get("manufacturer")})
        self.comboBox_System_Hersteller.blockSignals(True)
        self.comboBox_System_Hersteller.clear()
        self.comboBox_System_Hersteller.addItems(man_names)
        self.comboBox_System_Hersteller.blockSignals(False)
        self.comboBox_System_Hersteller.currentIndexChanged.connect(self._on_manufacturer_change)
        
        self._populate_hardware_comboboxes()

        if self.comboBox_System_Hersteller.count():
            self._on_manufacturer_change()      # stellt PV-System & WR passend ein

        self.comboBox_System_PV_System.currentIndexChanged.connect(
            self._on_system_change
        )
        self.comboBox_System_Inverter.currentIndexChanged.connect(
            self._on_inverter_change
        )

        # erster System-Change auslösen
        self.comboBox_System_PV_System.setCurrentIndex(0)
        self._on_system_change()
        
        # Änderungen an den Haupt-Combo-Boxen auslösen
        self.comboBox_System_Hersteller.currentTextChanged.connect(self._update_system_overview)
        self.comboBox_System_PV_System .currentTextChanged.connect(self._update_system_overview)
        self.comboBox_System_Inverter.currentTextChanged.connect(self._update_system_overview)

        # Für jede bereits vorhandene Generator-Page die Spin-Boxen verbinden
        for i in range(self.stackedWidget_Generator.count()):
            page = self.stackedWidget_Generator.widget(i)
            for name in ("spinBox_Modulanzahl_Generator_", "spinBox_Leistung_Generator_"):
                sp = page.findChild(QtWidgets.QSpinBox, name)
                if sp:
                    sp.valueChanged.connect(self._update_system_overview)

        # ------------------------------------------------------------
        #   8) Live-Kosten-Anzeige – Signale verdrahten
        # ------------------------------------------------------------
        for obj_name in [
            # Kosten-Eingaben
            "doubleSpinBox_Kosten_Modulkosten",
            "doubleSpinBox_Kosten_Wechselrichter",
            "doubleSpinBox_Kosten_Speicherkosten",
            "doubleSpinBox_Kosten_Installationskosten",
            "doubleSpinBox_Kosten_Foerderung",
            # Speicher-Anzahl
            "spinBox_System_Speichermodule",
        ]:
            w = self.findChild(QtWidgets.QAbstractSpinBox, obj_name)
            if w:
                w.valueChanged.connect(self._update_cost_display)

        # erste Berechnung sofort durchführen
        self._update_cost_display()

        # ------------------------------------------------------------
        #   9) Standort-Buttons
        # ------------------------------------------------------------
        self.pushButton_Standort_zuAdresse.clicked.connect(
            self._reverse_geocode
        )
        self.pushButton_Standort_zuKoordinaten.clicked.connect(
            self._geocode_address
        )
        # Live-Karte aktualisieren, sobald sich Koordinaten ändern
        self.doubleSpinBox_Standort_Breitengrad.valueChanged.connect(self._update_map)
        self.doubleSpinBox_Standort_Laengengrad.valueChanged.connect(self._update_map)
        # Nach Geocode-Aktionen auch neu zentrieren
        self.pushButton_Standort_zuAdresse.clicked.connect(self._update_map)
        self.pushButton_Standort_zuKoordinaten.clicked.connect(self._update_map)

        # ------------------------------------------------------------
        #  10) Navigations-Buttons (Tabs)
        # ------------------------------------------------------------
        self.pushButton_to_Tab_Anlage.clicked.connect(
            lambda *_: self.tabWidget.setCurrentIndex(1)
        )
        # self.pushButton_to_Tab_Kosten.clicked.connect(
        #     lambda *_: self.tabWidget.setCurrentIndex(2)
        # )
        self.pushButton_back_to_Tab_Ort.clicked.connect(
            lambda *_: self.tabWidget.setCurrentIndex(0)
        )
        # self.pushButton_back_to_Tab_Anlage.clicked.connect(
        #     lambda *_: self.tabWidget.setCurrentIndex(1)
        # )

        # # ------------------------------------------------------------
        # #  11) Generator-Tabs (Neu / Löschen)
        # # ------------------------------------------------------------
        self.pushButton_neuer_Generator.clicked.connect(self._add_generator_page)
        self.pushButton_Generator_losechen.clicked.connect(self._remove_current_generator)
        
        # im __init__-Block nach self.listWidget_Generator … hinzufügen
        self.listWidget_Generator.currentRowChanged.connect(
            self.stackedWidget_Generator.setCurrentIndex
        )
        self.listWidget_Generator.currentRowChanged.connect(
            lambda idx: self._toggle_shade_mode(
                self.stackedWidget_Generator.widget(idx)
                if idx >= 0 else None
            )
        )

        # --- Template entfernen ----------------------------------------
        self._template_page = self.stackedWidget_Generator.widget(0)
        self.stackedWidget_Generator.removeWidget(self._template_page)

        self._update_generator_widgets_enabled(False)   # jetzt erst deaktivieren

        # ------------------------------------------------------------
        #  Generator-Tab-Widgets initialisieren
        # ------------------------------------------------------------
        if self.listWidget_Generator.count():
            self.listWidget_Generator.setCurrentRow(0)
        
        # ------------------------------------------------------------
        self._add_generator_page()
        self._update_system_overview()
    
    # ----------------------------------------------------------------
    # Klick-Handler-Funktionen für die Karte
    # ----------------------------------------------------------------
    def _on_map_clicked(self, lat: float, lon: float) -> None:
        """Wird aufgerufen, wenn der User auf die Karte klickt."""
        # SpinBoxes setzen (ohne rekursives _update_map)
        self.doubleSpinBox_Standort_Breitengrad.blockSignals(True)
        self.doubleSpinBox_Standort_Laengengrad.blockSignals(True)
        self.doubleSpinBox_Standort_Breitengrad.setValue(lat)
        self.doubleSpinBox_Standort_Laengengrad.setValue(lon)
        self.doubleSpinBox_Standort_Breitengrad.blockSignals(False)
        self.doubleSpinBox_Standort_Laengengrad.blockSignals(False)
        # Karte neu zentrieren und Geocode-Buttons etc.
        self._update_map()

    def _on_manufacturer_change(self) -> None:
        man = self.comboBox_System_Hersteller.currentText()

        # alle Systeme des Herstellers ausser dem „generischen“
        systems = [
            s for s in _pv_systems
            if s["manufacturer"] == man and s["name"].strip().lower() != man.lower()
        ]

        self.comboBox_System_PV_System.blockSignals(True)
        self.comboBox_System_PV_System.clear()

        if systems:                                  # ► mehrere Auswahl-möglichkeiten
            for sys_obj in systems:
                label = sys_obj["name"].removeprefix(f"{man} ").lstrip()
                self.comboBox_System_PV_System.addItem(label, userData=sys_obj["name"])
            self.comboBox_System_PV_System.setEnabled(True)
        else:                                        # ► nur 1 „generisches“ System
            # dieses eine System in die Box eintragen und die Box deaktivieren
            gen_sys = next(s for s in _pv_systems if s["manufacturer"] == man)
            self.comboBox_System_PV_System.addItem(gen_sys["name"], userData=gen_sys["name"])
            self.comboBox_System_PV_System.setEnabled(False)

        self.comboBox_System_PV_System.blockSignals(False)

        # nach dem Befüllen sofort abhängige Comboboxen aktualisieren
        self._on_system_change()

    # ----------------------------------------------------------------
    # ➋ »Über BKWSimX«
    # ----------------------------------------------------------------
    def _show_about(self) -> None:
        text = (
            f"<b>BKWSimX {__version__}</b><br>"
            "Simulation und Planung steckerfertiger PV-Anlagen<br><br>"
            "Copyright © 2025 Martin Teske<br>"
            "<a href='https://www.martinteske-blog.de'>martinteske-blog.de</a>"
        )
        QMessageBox.about(self, "Über BKWSimX", text)

    # ----------------------------------------------------------------
    # ➌ »Rechtliches / Lizenzen«
    # ----------------------------------------------------------------
    def _show_legal(self) -> None:
        text = (
            f"<b>BKWSimX {__version__} – Open-Source-Software (MIT-Lizenz)</b><br><br>"
            "© 2025 Martin Teske<br>"
            "Website: <a href='https://www.martinteske-blog.de'>martinteske-blog.de</a><br><br>"
            "<b>Verwendete Komponenten und Lizenzen:</b><br>"
            "• Python 3 (PSF License) – <a href='https://docs.python.org/3/license.html'>Lizenz</a><br>"
            "• PyQt6 (GPL v3 oder kommerzielle Lizenz) – Riverbank Computing<br>"
            "• QtWebEngine / QtWebChannel (GPL v3) – Bestandteil von PyQt6<br>"
            "• pvlib (BSD 3-Clause) – <a href='https://github.com/pvlib/pvlib-python/blob/master/LICENSE'>Lizenz</a><br>"
            "• pandas (BSD 3-Clause) – <a href='https://pandas.pydata.org/licensing.html'>Lizenz</a><br>"
            "• NumPy (BSD 3-Clause) – <a href='https://numpy.org/license.html'>Lizenz</a><br>"
            "• matplotlib (PSF/BSD-kompatibel) – <a href='https://matplotlib.org/stable/users/license.html'>Lizenz</a><br>"
            "• mplcursors (MIT) – <a href='https://pypi.org/project/mplcursors/'>Lizenz</a><br>"
            "• screeninfo (MIT) – <a href='https://pypi.org/project/screeninfo/'>Lizenz</a><br>"
            "• requests (Apache 2.0) – <a href='https://pypi.org/project/requests/'>Lizenz</a><br><br>"
            "<b>Externe Dienste und Datenquellen:</b><br>"
            "• PV-Ertragsdaten von <a href='https://ec.europa.eu/jrc/en/pvgis'>PVGIS</a> (Europäische Kommission)<br>"
            "• Kartendaten von <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap-Mitwirkenden</a> – ODbL 1.0 Lizenz<br><br>"
            "<b>Haftungsausschluss:</b><br>"
            "BKWSimX wird ohne jegliche Gewährleistung bereitgestellt. Obwohl alle Berechnungen mit größter Sorgfalt erfolgen, "
            "übernimmt der Autor keine Haftung für die Richtigkeit, Vollständigkeit oder Aktualität der Ergebnisse. "
            "Die Nutzung erfolgt auf eigene Verantwortung.<br><br>"
            "Diese Software ist freie Software: Sie dürfen sie unter den Bedingungen der MIT-Lizenz nutzen, ändern und weiterverbreiten."
        )
        QMessageBox.information(self,
            f"Rechtliches & Lizenzen – BKWSimX {__version__}",
            text
        )

    # ------------------------------------------------------------------
    # Eingaben einsammeln
    # ------------------------------------------------------------------
    def _collect_settings(self) -> Settings:
        """Liest **alle** GUI‑Felder und baut ein `Settings`‑Objekt."""

        # Standort
        lat = self.doubleSpinBox_Standort_Breitengrad.value()  # type: ignore[attr-defined]
        lon = self.doubleSpinBox_Standort_Laengengrad.value()  # type: ignore[attr-defined]

        # --- neue Implementierung ---
        mppts: list[GeneratorConfig] = []
        for page_idx in range(self.stackedWidget_Generator.count()):
            page = self.stackedWidget_Generator.widget(page_idx)
            n_mod   = page.findChild(QtWidgets.QSpinBox, "spinBox_Modulanzahl_Generator_").value()
            conn    = page.findChild(QtWidgets.QComboBox, "comboBox_Verschaltung_Generator_").currentText().lower()
            wp_mod  = page.findChild(QtWidgets.QSpinBox, "spinBox_Leistung_Generator_").value()
            tilt    = page.findChild(QtWidgets.QSpinBox, "spinBox_Neigung_Generator_").value()
            azm     = page.findChild(QtWidgets.QSpinBox, "spinBox_Azimut_Generator_").value()
            mppt_id = page.findChild(QtWidgets.QComboBox, "comboBox_MPPT_Input").currentIndex() + 1
            sh_mode = ("monatlich"
                    if page.radioButton_Shade_Monthly.isChecked()
                    else "einfach")
            sh_lvl  = page.comboBox_Shade_Level.currentText()
            sh_mon  = {
                m: page.findChild(QtWidgets.QSpinBox, f"spinBox_Verschattung_{_month_abbr(m)}").value()
                for m in range(1,13)
            }
            mppts.append(GeneratorConfig(
                mppt_index = mppt_id,
                n_modules   = n_mod,
                connection  = "direct" if conn.startswith("direkt") else "series",
                wp_module   = wp_mod,
                tilt_deg    = tilt,
                azimuth_deg = azm,
                shading_mode        = sh_mode,
                shading_simple_lvl  = sh_lvl,
                shading_monthly_pct = sh_mon,
            ))  

        # Verluste (%)
        losses = {}
        loss_candidates = (
            ("Leitungsverluste",        "doubleSpinBox_Verluste_Leitungsverluste"),
            ("Verschmutzung",           "doubleSpinBox_Verluste_Verschmutzung"),
            ("Modul‑Mismatch",          "doubleSpinBox_Verluste_Modul_Mismatch"),
            ("LID",                     "doubleSpinBox_Verluste_LID"),
            ("Nameplate‑Toleranz",      "doubleSpinBox_Verluste_Nameplate_Toleranz"),
            ("Alterung",                "doubleSpinBox_Verluste_Alterung"),
        )
        for label, obj in loss_candidates:
            w = self.findChild(QtWidgets.QDoubleSpinBox, obj)
            if w is not None:
                losses[label] = w.value()

        # Monats‑Verschattung
        monthly_shade = {
            m: self.findChild(QtWidgets.QSpinBox, f"spinBox_Verschattung_{_month_abbr(m)}").value()  # type: ignore[arg-type]
            for m in range(1, 13)
        }

        settings = Settings(
            latitude=lat,
            longitude=lon,
            manufacturer   = self.comboBox_System_Hersteller.currentText(),
            system_name=self.comboBox_System_PV_System.currentData()
                        or self.comboBox_System_PV_System.currentText(),  # type: ignore[attr-defined]
            inverter_model=self.comboBox_System_Inverter.currentText(),  # type: ignore[attr-defined]
            battery_model=self.comboBox_System_Speichertyp.currentText(),  # type: ignore[attr-defined]
            batt_units     = self.spinBox_System_Speichermodule.value(),
            soc_min_pct=self.spinBox_System_SOC_min.value(),  # type: ignore[attr-defined]
            soc_max_pct=self.spinBox_System_SOC_max.value(),  # type: ignore[attr-defined]
            mppts=mppts,
            annual_load_kwh=self.spinBox_Jahesverbrauch.value(),  # type: ignore[attr-defined]
            profile="worker" if self.radioButton_Profil_Berufstaetig.isChecked() else "retiree",  # type: ignore[attr-defined]
            optimize_storage = self.checkBox_Speicheropt.isChecked(),   # NEU
            losses_pct=losses,
            cost_module_eur=self.doubleSpinBox_Kosten_Modulkosten.value(),  # type: ignore[attr-defined]
            cost_inverter_eur=self.doubleSpinBox_Kosten_Wechselrichter.value(),  # type: ignore[attr-defined]
            cost_install_eur=self.doubleSpinBox_Kosten_Installationskosten.value(),  # type: ignore[attr-defined]
            cost_battery_eur=self.doubleSpinBox_Kosten_Speicherkosten.value(),  # type: ignore[attr-defined]
            subsidy_eur=self.doubleSpinBox_Kosten_Foerderung.value(),  # type: ignore[attr-defined]
            price_eur_per_kwh=self.doubleSpinBox_Kosten_Strompreis.value(),  # type: ignore[attr-defined]
            price_escalation_pct=self.doubleSpinBox_Kosten_Strompreissteigerung.value(),  # type: ignore[attr-defined]
            operating_years=int(self.doubleSpinBox_Kosten_Betriebszeit.value()),  # type: ignore[attr-defined]
            co2_factor=self.doubleSpinBox_Kosten_CO2.value(),  # type: ignore[attr-defined]
        )
        return settings

    # ------------------------------------------------------------------
    # Comboboxen initial befüllen
    # ------------------------------------------------------------------
    def _populate_hardware_comboboxes(self) -> None:
        # PV-Systeme (Text=sys["name"], Data=sys["id"])
        self.comboBox_System_PV_System.clear()
        for sys in _pv_systems:
            self.comboBox_System_PV_System.addItem(sys["name"], sys["id"])

        # Inverter
        self.comboBox_System_Inverter.clear()
        for inv in _inverters:
            self.comboBox_System_Inverter.addItem(inv["model"], inv["id"])

        # Batterie
        self.comboBox_System_Speichertyp.clear()
        for bat in _batteries:
            self.comboBox_System_Speichertyp.addItem(bat["model"], bat["id"])

    # ------------------------------------------------------------------
    # System gewechselt  →  WR- & Batterie-Auswahl anpassen
    # ------------------------------------------------------------------
    def _on_system_change(self) -> None:
        key = (self.comboBox_System_PV_System.currentData()
            or self.comboBox_System_PV_System.currentText())
        if not key:                # Liste leer → nichts zu tun
            return
        sys_obj = _sys_by_name[key]

        # ----- Wechselrichter‑Combobox ---------------------------------
        if sys_obj.get("inverter_integrated"):
            # integrierter WR → Combobox deaktivieren
            self.comboBox_System_Inverter.blockSignals(True)
            self.comboBox_System_Inverter.clear()
            self.comboBox_System_Inverter.addItem("integriert")
            self.comboBox_System_Inverter.setEnabled(False)
            self.comboBox_System_Inverter.blockSignals(False)
        else:
            inv_ids = sys_obj.get("supported_inverter_types", [])
            inv_models = [_inv_by_id[i]["model"] for i in inv_ids]
            self.comboBox_System_Inverter.blockSignals(True)
            self.comboBox_System_Inverter.setEnabled(True)
            self.comboBox_System_Inverter.clear()
            self.comboBox_System_Inverter.addItems(inv_models)
            self.comboBox_System_Inverter.setCurrentIndex(0)
            self.comboBox_System_Inverter.blockSignals(False)

        # ----- Batterie‑Combobox + Speicher‑Felder ---------------------
        if sys_obj.get("storage_supported"):
            batt_ids = sys_obj.get("supported_storage_types", [])
            batt_models = [_batt_by_id[i]["model"] for i in batt_ids]
            self.comboBox_System_Speichertyp.blockSignals(True)
            self.comboBox_System_Speichertyp.setEnabled(True)
            self.comboBox_System_Speichertyp.clear()
            self.comboBox_System_Speichertyp.addItems(batt_models)
            self.comboBox_System_Speichertyp.setCurrentIndex(0)
            self.comboBox_System_Speichertyp.blockSignals(False)

            # Speicher‑Einstellungen aktivieren
            self.spinBox_System_Speichermodule.setEnabled(True)
            self.spinBox_System_SOC_min.setEnabled(True)
            self.spinBox_System_SOC_max.setEnabled(True)
            self.checkBox_Speicheropt.setEnabled(True)
        else:
            # kein Speicher unterstützt
            self.comboBox_System_Speichertyp.blockSignals(True)
            self.comboBox_System_Speichertyp.clear()
            self.comboBox_System_Speichertyp.addItem("—")
            self.comboBox_System_Speichertyp.setEnabled(False)
            self.comboBox_System_Speichertyp.blockSignals(False)

            self.spinBox_System_Speichermodule.setValue(0)
            self.spinBox_System_Speichermodule.setEnabled(False)
            self.spinBox_System_SOC_min.setEnabled(False)
            self.spinBox_System_SOC_max.setEnabled(False)
            self.checkBox_Speicheropt.setEnabled(False)

        # ----- MPPT‑Eingabefelder neu erzeugen -------------------------
        self._rebuild_mppt_fields(sys_obj)
        
    # -----------------------------------------------------------
    # Generator‑Page (PV)
    # -----------------------------------------------------------
    def _update_generator_connection_options(self, page: QtWidgets.QWidget) -> None:
        """Setzt in dieser Generator-Page die Verschaltungs-Optionen je nach Modulanzahl."""
        spn_n = page.findChild(QtWidgets.QSpinBox, "spinBox_Modulanzahl_Generator_")
        cb_conn = page.findChild(QtWidgets.QComboBox, "comboBox_Verschaltung_Generator_")
        if not (spn_n and cb_conn):
            return
        n = spn_n.value()
        cb_conn.blockSignals(True)
        cb_conn.clear()
        if n <= 1:
            # nur 1 Modul → nur direkte Verschaltung möglich
            cb_conn.addItem("direkt")
            cb_conn.setEnabled(False)
        else:
            # >1 Module → beides erlauben
            cb_conn.addItems(["direkt", "reihe"])
            cb_conn.setEnabled(True)
        cb_conn.blockSignals(False)
    # ──── BLOCK B · START ───────────────────────────────────────────────
    def _add_generator_page(self) -> None:
        """Erzeugt eine neue Generator-Seite + List-Eintrag."""
        ui_gen = Path(__file__).resolve().parent.parent / "ui" / "pv_generator_page.ui"
        page   = uic.loadUi(ui_gen)                     # Seite laden
        
        # --- Visualisierungs-Widgets verdrahten ---
        # Neigungs-Frame (seitliche Ansicht)
        from gui.widgets import TiltWidget, AzimuthWidget
        tilt_frame: TiltWidget = page.findChild(TiltWidget, "frame_neigung")
        spin_tilt = page.findChild(QtWidgets.QSpinBox, "spinBox_Neigung_Generator_")
        if tilt_frame and spin_tilt:
            spin_tilt.valueChanged.connect(tilt_frame.setAngle)
            # Initialwert setzen
            tilt_frame.setAngle(spin_tilt.value())

        # Azimut-Frame (Draufsicht)
        azi_frame: AzimuthWidget = page.findChild(AzimuthWidget, "frame_azimut")
        spin_azi = page.findChild(QtWidgets.QSpinBox, "spinBox_Azimut_Generator_")
        if azi_frame and spin_azi:
            spin_azi.valueChanged.connect(azi_frame.setAzimuth)
            # Initialwert setzen
            azi_frame.setAzimuth(spin_azi.value())

        # ────────────────────────────────────────────────────────────────
        # 1) MPPT-Eingänge füllen
        # ---------------------------------------------------------------
        # aktuellen Wechselrichter auslesen und über _inv_by_model finden
        inv_model = self.comboBox_System_Inverter.currentText()
        inv = _inv_by_model.get(inv_model, {})
        # angenommen in deinem JSON heißt das Feld "mppt_inputs" oder ähnlich
        mppt_count = inv.get("mppt_inputs", 1)
        cb_mppt = page.findChild(QtWidgets.QComboBox, "comboBox_MPPT_Input")
        cb_mppt.clear()
        # Einträge "1", "2", ... bis mppt_count
        for i in range(1, mppt_count + 1):
            cb_mppt.addItem(str(i))

        # ────────────────────────────────────────────────────────────────
        # 2) Schattierungs­level füllen
        # ---------------------------------------------------------------
        # nur für den Modus "Einfach" relevant
        cb_shade = page.findChild(QtWidgets.QComboBox, "comboBox_Shade_Level")
        cb_shade.clear()
        cb_shade.addItems(["keine", "leicht", "mittel", "stark"])

        # ────────────────────────────────────────────────────────────────
        # 2a) Verschaltungs-Optionen je nach Modulanzahl setzen
        # ---------------------------------------------------------------
        self._update_generator_connection_options(page)
        # Modul-Spinbox ändert Verschaltung und Übersicht
        spn_n = page.findChild(QtWidgets.QSpinBox, "spinBox_Modulanzahl_Generator_")
        if spn_n:
            spn_n.valueChanged.connect(lambda val, p=page: (
                self._update_generator_connection_options(p),
                self._update_system_overview()
            ))

        # Leistung-Spinbox → Übersicht
        spn_wp = page.findChild(QtWidgets.QSpinBox, "spinBox_Leistung_Generator_")
        if spn_wp:
            spn_wp.valueChanged.connect(self._update_system_overview)

        # MPPT-Auswahl → Übersicht
        cb_mppt = page.findChild(QtWidgets.QComboBox, "comboBox_MPPT_Input")
        if cb_mppt:
            cb_mppt.currentIndexChanged.connect(self._update_system_overview)

        # Verschaltung → Übersicht
        cb_conn = page.findChild(QtWidgets.QComboBox, "comboBox_Verschaltung_Generator_")
        if cb_conn:
            cb_conn.currentIndexChanged.connect(self._update_system_overview)
            
        self.comboBox_System_Speichertyp.currentTextChanged.connect(self._update_system_overview)
        self.spinBox_System_Speichermodule.valueChanged.connect(self._update_system_overview)
        self.spinBox_System_SOC_min.valueChanged.connect(self._update_system_overview)
        self.spinBox_System_SOC_max.valueChanged.connect(self._update_system_overview)
        # ────────────────────────────────────────────────────────────────
        # Rest deiner Methode
        # ---------------------------------------------------------------
        self.stackedWidget_Generator.addWidget(page)

        idx = self.stackedWidget_Generator.count() - 1
        self.listWidget_Generator.addItem(f"Generator {idx+1}")
        self.listWidget_Generator.setCurrentRow(idx)

        # Generator-Bereich wieder aktivieren
        self._update_generator_widgets_enabled(True)

        # Radio-Buttons verdrahten
        page.radioButton_Shade_Simple.toggled.connect(
            lambda *_: self._toggle_shade_mode(page)
        )
        page.radioButton_Shade_Monthly.toggled.connect(
            lambda *_: self._toggle_shade_mode(page)
        )

        # MPPT-Felder & Shade-Status initial anpassen
        self._rebuild_mppt_fields_current_page(page)
        self._toggle_shade_mode(page)
        
        # Spin-Boxen der neuen Seite verbinden
        for name in ("spinBox_Modulanzahl_Generator_", "spinBox_Leistung_Generator_"):
            sp = page.findChild(QtWidgets.QSpinBox, name)
            if sp:
                sp.valueChanged.connect(self._update_system_overview)

        # Übersicht sofort einmal aktualisieren
        self._update_system_overview()

    def _toggle_shade_mode(self, page: QtWidgets.QWidget | None) -> None:
        """
        Aktiviert/Deaktiviert monatliche Verschattungs-Spinboxen
        **nur in der übergebenen Page** (oder – falls None – in keiner).
        """
        if page is None:
            return                                    # z. B. wenn Liste leer ist

        simple = page.radioButton_Shade_Simple.isChecked()
        page.comboBox_Shade_Level.setEnabled(simple)

        for m in range(1, 13):
            sp = page.findChild(QtWidgets.QSpinBox,
                                f"spinBox_Verschattung_{_month_abbr(m)}")
            if sp:
                sp.setEnabled(not simple)

    def _rebuild_mppt_fields_current_page(self, page: QtWidgets.QWidget) -> None:
        """
        Füllt *in dieser Generator-Page* die MPPT-Combobox anhand der
        aktuell gewählten Wechselrichter-/System-Daten.
        """
        # Wie viele MPPT-Eingänge sind verfügbar?
        key = (self.comboBox_System_PV_System.currentData()
            or self.comboBox_System_PV_System.currentText())
        sys_obj = _sys_by_name[key]

        if sys_obj.get("inverter_integrated"):
            n_mppt = sys_obj.get("mppt_inputs", 1)
        else:
            inv_model = self.comboBox_System_Inverter.currentText()
            inv_obj   = _inv_by_model.get(inv_model, {})
            n_mppt     = inv_obj.get("mppt_inputs", 1)

        # Combobox in der Page suchen und befüllen
        box = page.findChild(QtWidgets.QComboBox, "comboBox_MPPT_Input")
        if box:
            box.blockSignals(True)
            box.clear()
            box.addItems([str(i) for i in range(1, n_mppt + 1)])
            box.setCurrentIndex(0)
            box.blockSignals(False)

    def _remove_current_generator(self) -> None:
        row = self.listWidget_Generator.currentRow()
        if row == 0:
            QMessageBox.warning(self, "Löschen", "Generator 1 kann nicht gelöscht werden.")
            return
        self.stackedWidget_Generator.removeWidget(
            self.stackedWidget_Generator.widget(row)
        )
        self.listWidget_Generator.takeItem(row)

        # Reihen neu durchnummerieren
        for i in range(self.listWidget_Generator.count()):
            self.listWidget_Generator.item(i).setText(f"Generator {i+1}")

        # Auswahl korrigieren
        self.listWidget_Generator.setCurrentRow(max(0, row-1))
        self._update_generator_widgets_enabled(
            self.stackedWidget_Generator.count() > 0
        )
        
        self._update_system_overview()
        return

    def _update_generator_widgets_enabled(self, enable: bool) -> None:
        """Blendet den ganzen Generator-Bereich ein/aus, wenn (k)ein Page existiert."""
        self.listWidget_Generator.setEnabled(enable)
        self.stackedWidget_Generator.setEnabled(enable)

    # ------------------------------------------------------------------
    # Nur MPPT-Zahl aktualisieren, falls externer WR geändert wird
    # ------------------------------------------------------------------
    def _on_inverter_change(self) -> None:
        key = (self.comboBox_System_PV_System.currentData()
            or self.comboBox_System_PV_System.currentText())
        sys_obj = _sys_by_name[key]
        if not sys_obj.get("inverter_integrated"):
            self._rebuild_mppt_fields(sys_obj)

    # ------------------------------------------------------------------
    # MPPT‑Widgets für nicht benötigte Tracker deaktivieren
    # ------------------------------------------------------------------
    def _rebuild_mppt_fields(self, sys_obj) -> None:
        """Aktiviert genau so viele MPPT‑Zeilen, wie das System benötigt."""
        # ▶ Guard: nichts tun, wenn noch kein Generator-Page existiert
        current = self.stackedWidget_Generator.currentWidget()
        if current is None:
            return
        
        if sys_obj.get("inverter_integrated"):
            n_mppt = sys_obj.get("mppt_inputs", 1)
        else:
            inv_model = self.comboBox_System_Inverter.currentText()
            inv_obj = _inv_by_model.get(inv_model, {})
            n_mppt = inv_obj.get("mppt_inputs", 1)

        # Alle Spinboxen & Labels, die zu einem MPPT gehören, heißen
        #  spinBox_PV_Generator_MPPT<i>_…   bzw.   …_Module_2 (UI‑Tippfehler)
        for i in range(1, 5):
            enabled = i <= n_mppt
            # jede SpinBox innerhalb des Frames inspizieren
            #for sp in self.findChildren(QtWidgets.QSpinBox):
            for sp in current.findChildren(QtWidgets.QSpinBox):
                if f"_MPPT{i}_" in sp.objectName():
                    sp.setEnabled(enabled)
                    if not enabled:
                        sp.setValue(0)            # verhindert ungewollte Modul‑Einträge
            # Tippfehler‑Variante für Modul‑SpinBox von MPPT 2
            weird = self.findChild(QtWidgets.QSpinBox, f"spinBox_PV_Generator_MPPT1_Module_{i}")
            if weird:
                weird.setEnabled(enabled)
            # Label ebenso
            lbl = self.findChild(QtWidgets.QLabel, f"label_PV_Generator_MPPT{i}")
            if lbl:
                lbl.setEnabled(enabled)

        # --- NEU: über alle Pages laufen -------------------------------
        for page_idx in range(self.stackedWidget_Generator.count()):
            page = self.stackedWidget_Generator.widget(page_idx)

            # neue eindeutige MPPT-Combobox
            box = page.findChild(QtWidgets.QComboBox, "comboBox_MPPT_Input")
            if box:
                box.blockSignals(True)
                box.clear()
                box.addItems([str(i) for i in range(1, n_mppt + 1)])
                box.setCurrentIndex(0)
                box.blockSignals(False)
        
    # ------------------------------------------------------------------
    # Koordinaten  →  Adresse
    # ------------------------------------------------------------------
    def _reverse_geocode(self) -> None:
        # alle bisherigen Adressfelder leeren
        for name in ("lineEdit_Standort_Land",
                    "lineEdit_Standort_PLZ",
                    "lineEdit_Standort_Ort",
                    "lineEdit_Standort_Strasse",
                    "lineEdit_Standort_Hausnummer"):
            le = self.findChild(QtWidgets.QLineEdit, name)
            if le:
                le.clear()

        # dann wie gewohnt reverse geocoden
        try:
            lat = self.doubleSpinBox_Standort_Breitengrad.value()
            lon = self.doubleSpinBox_Standort_Laengengrad.value()
            r = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json"},
                headers=HEADERS, timeout=8,
            )
            r.raise_for_status()
            data = r.json().get("address", {})
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Reverse Geocoding", str(exc))
            return

        # Felder nur überschreiben, wenn leer
        def _set(le_name: str, val: str) -> None:
            le: QtWidgets.QLineEdit = self.findChild(QtWidgets.QLineEdit, le_name)  # type: ignore
            if le:
                le.setText(val)

        _set("lineEdit_Standort_Land",       data.get("country", ""))
        _set("lineEdit_Standort_PLZ",        data.get("postcode", ""))
        _set("lineEdit_Standort_Ort",        data.get("city") or data.get("town") or data.get("village", ""))
        _set("lineEdit_Standort_Strasse",    data.get("road", ""))
        _set("lineEdit_Standort_Hausnummer", data.get("house_number", ""))

    # ------------------------------------------------------------------
    # Adresse  →  Koordinaten
    # ------------------------------------------------------------------
    def _geocode_address(self) -> None:
        street = f"{self.lineEdit_Standort_Hausnummer.text().strip()} {self.lineEdit_Standort_Strasse.text().strip()}".strip()
        params = {
            "street":      street,
            "city":        self.lineEdit_Standort_Ort.text(),
            "postalcode":  self.lineEdit_Standort_PLZ.text(),
            "country":     self.lineEdit_Standort_Land.text(),
            "format":      "json",
            "limit":       1,
        }
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params=params, headers=HEADERS, timeout=8)
            r.raise_for_status()
            data = r.json()
            if not data:
                raise ValueError("keine Treffer")
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
            self.doubleSpinBox_Standort_Breitengrad.setValue(lat)
            self.doubleSpinBox_Standort_Laengengrad.setValue(lon)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Geocoding", f"Adresse nicht gefunden:\n{exc}")

    # ------------------------------------------------------------------
    #   Live-Kosten-Anzeige
    # ------------------------------------------------------------------
    def _update_cost_display(self) -> None:
        """Addiert alle Kosten-Eingaben und aktualisiert die
        vier Read-only-Felder im Kosten-Tab."""
        mod_cost = self.doubleSpinBox_Kosten_Modulkosten.value()
        wr_cost  = self.doubleSpinBox_Kosten_Wechselrichter.value()
        bat_cost = (
            self.doubleSpinBox_Kosten_Speicherkosten.value()
            * self.spinBox_System_Speichermodule.value()
        )
        inst_cost = self.doubleSpinBox_Kosten_Installationskosten.value()
        subsidy   = self.doubleSpinBox_Kosten_Foerderung.value()

        hw_total  = mod_cost + wr_cost + bat_cost
        grand_tot = hw_total + inst_cost - subsidy

        self.doubleSpinBox_Anzeige_Hardwarekosten.setValue(hw_total)
        self.doubleSpinBox_Anzeige_Installationskosten.setValue(inst_cost)
        self.doubleSpinBox_Anzeige_Foerderungen.setValue(subsidy)
        self.doubleSpinBox_Anzeige_Gesamt.setValue(grand_tot)

    # ------------------------------------------------------------------
    # Ergebnis‑Tab initialisieren
    # ------------------------------------------------------------------
    def _populate_manufacturer_box(self) -> None:
        """Füllt comboBox_System_Hersteller mit allen distinct‑Herstellern."""
        manufacturers = sorted({s["manufacturer"] for s in _pv_systems})
        self.comboBox_System_Hersteller.clear()
        self.comboBox_System_Hersteller.addItems(manufacturers)
    
    # -------------------------------------------------------------------
    # System-Übersicht aktualisieren  (Anlage- & Ergebnis-Tab)
    # -------------------------------------------------------------------
    def _update_system_overview(self) -> None:
        # ------------------------------------------------------------
        # 0) Grundobjekte laden (System- & Wechselrichter-Dicts)
        # ------------------------------------------------------------
        sys_key = (
            self.comboBox_System_PV_System.currentData()
            or self.comboBox_System_PV_System.currentText()
        )
        sys_obj = _sys_by_name.get(sys_key, {})

        if sys_obj.get("inverter_integrated"):
            inv_model = "integriert"
            inv_obj   = {}
        else:
            inv_model = self.comboBox_System_Inverter.currentText()
            inv_obj   = _inv_by_model.get(inv_model, {})

        # ------------------------------------------------------------
        # 1) MPPT-Anzahl und Label im Anlage-Tab
        # ------------------------------------------------------------
        n_mppt = sys_obj.get("mppt_inputs") or inv_obj.get("mppt_inputs", 1)
        self.label_StringConfig_MPPT_Inputs_Text.setText(f"{n_mppt} Eingänge")

        # ------------------------------------------------------------
        # 2) Generator-Infos einsammeln  +  self._generator_configs füllen
        # ------------------------------------------------------------
        mppt_map: dict[int, list[str]] = {i: [] for i in range(1, n_mppt + 1)}
        self._generator_configs = []          # ← vor jedem Durchlauf neu aufbauen

        total_modules = 0
        total_power   = 0            # Wp gesamt

        for idx in range(self.stackedWidget_Generator.count()):
            page = self.stackedWidget_Generator.widget(idx)

            # ---------- Widgets sicher finden ------------------------
            spn_n  = page.findChild(QtWidgets.QSpinBox,
                                    "spinBox_Modulanzahl_Generator_")
            spn_wp = page.findChild(QtWidgets.QSpinBox,
                                    "spinBox_Leistung_Generator_")
            if spn_n is None or spn_wp is None:
                # Seite ist noch ein Platzhalter ohne Eingabefelder → überspringen
                continue

            n_mod  = spn_n.value()
            wp_mod = spn_wp.value()

            tilt = page.findChild(QtWidgets.QSpinBox,
                                "spinBox_Neigung_Generator_")
            azm  = page.findChild(QtWidgets.QSpinBox,
                                "spinBox_Azimut_Generator_")
            cb_conn = page.findChild(QtWidgets.QComboBox,
                                    "comboBox_Verschaltung_Generator_")
            cb_mppt = page.findChild(QtWidgets.QComboBox,
                                    "comboBox_MPPT_Input")

            # ---------- Summen & Anzeige-Text ------------------------
            total_modules += n_mod
            total_power   += n_mod * wp_mod

            conn_txt = cb_conn.currentText().capitalize() if cb_conn else "Direkt"
            mppt_id  = (cb_mppt.currentIndex() + 1) if cb_mppt else 1

            mppt_map.setdefault(mppt_id, []).append(
                f"Generator {idx+1}: {n_mod}×{wp_mod} Wp ({conn_txt})"
            )

            # ---------- GeneratorConfig für Wirkungsgrad -------------
            self._generator_configs.append(
                GeneratorConfig(
                    mppt_index   = mppt_id,
                    n_modules    = n_mod,
                    connection   = "direct" if conn_txt.lower().startswith("direkt") else "series",
                    wp_module    = wp_mod,
                    tilt_deg     = tilt.value() if tilt else 35,
                    azimuth_deg  = azm.value()  if azm  else 0,
                )
            )

        # ------------------------------------------------------------
        # 3) Labels im Anlage-Tab
        # ------------------------------------------------------------
        self.label_StringConfig_Modulanzahl_Text.setText(f"{total_modules} Module")
        self.label_StringConfig_ModulleistungGes_Text.setText(f"{total_power} Wp")

        mppt_blocks = []
        for m in range(1, n_mppt + 1):
            lines = mppt_map.get(m, [])
            block = f"MPPT {m}\n" + ("\n".join(lines) if lines else "– keine –")
            mppt_blocks.append(block)
        self.label_StringConfig_MPPTConfig_Text.setText("\n\n".join(mppt_blocks))

        # ------------------------------------------------------------
        # 4) Labels im Ergebnis-Tab – System-Teil
        # ------------------------------------------------------------
        self.label_Ergebnis_Hersteller_Text.setText(
            self.comboBox_System_Hersteller.currentText()
        )
        self.label_Ergebnis_System_Text.setText(sys_key)
        self.label_Ergebnis_Inverter_Text.setText(inv_model)
        self.label_Ergebnis_PV_GeneratorWp_Text.setText(f"{total_power:.0f} Wp")

        # ------------------------------------------------------------
        # 5) Speicher-Labels (nur wenn ausgewählt)
        # ------------------------------------------------------------
        batt_model = self.comboBox_System_Speichertyp.currentText().strip()
        batt_count = self.spinBox_System_Speichermodule.value()

        if batt_model in _batt_by_model and batt_count > 0:
            specs = get_battery_spec(batt_model)

            brutto_wh = batt_count * specs["capacity_wh"]
            soc_min = self.spinBox_System_SOC_min.value() / 100
            soc_max = self.spinBox_System_SOC_max.value() / 100
            netto_wh = brutto_wh * (soc_max - soc_min)

            self.label_Ergebnis_Speichertyp_Text.setText(
                f"{batt_count}× {batt_model}"
            )
        else:
            # Platzhalter, falls kein gültiger Speicher konfiguriert
            self.label_Ergebnis_Speichertyp_Text.setText("—")
    
    # ------------------------------------------------------------------
    # OSM-Karte aktualisieren
    # ------------------------------------------------------------------
    def _update_map(self) -> None:
        """Zentriert Karte und Marker auf die aktuellen Koordinaten."""
        lat = self.doubleSpinBox_Standort_Breitengrad.value()
        lon = self.doubleSpinBox_Standort_Laengengrad.value()
        js = (
            f"marker.setLatLng([{lat}, {lon}]);"
            f"map.setView([{lat}, {lon}], map.getZoom());"
        )
        # führt das JavaScript in der WebEngine aus
        self.map_view.page().runJavaScript(js)
                       
    # ------------------------------------------------------------------
    # Simulation starten
    # ------------------------------------------------------------------

    def _start_calc(self) -> None:
        # 1) Leerzeile als Trennung in der Debug-Konsole
        self._debug_console.appendPlainText("")
        # 2) Trennung im Terminal-Log
        calc_logger = logging.getLogger("logic.calculation")
        calc_logger.debug("")

        # 3) Button-Klick-Log
        calc_mod.CURRENT_SCENARIO = None
        calc_logger.debug("Button 'Berechnen' gedrückt – beginne Berechnung")

        # ─────────────────────────────────────────────────
        #   Jetzt den „Bitte warten“-Dialog anlegen & zeigen
        # ─────────────────────────────────────────────────
        self._wait_dialog = QProgressDialog("Berechnung läuft…", "", 0, 0, self)
        self._wait_dialog.setWindowTitle("Bitte warten")
        self._wait_dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self._wait_dialog.setCancelButton(None)
        self._wait_dialog.show()
        
        self._btn_calc.setEnabled(False)
        base_settings = self._collect_settings()
        n_units = self.spinBox_System_Speichermodule.value()
        self._scenario_units = list(range(0, n_units + 1))
        self._build_result_model(len(self._scenario_units))
        self._worker = CalcWorker(base_settings, self._scenario_units)
        self._worker.finished.connect(self._on_result)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    # ------------------------------------------------------------------
    # Ergebnis‑Callback  –  alle Resultate in der Konsole ausgeben
    # ------------------------------------------------------------------
    @QtCore.pyqtSlot(dict)
    def _on_result(self, res_all: dict[int, dict]) -> None:
        # Dialog schließen
        self._wait_dialog.hide()
        res = res_all[0]
        if "loss_total_percent" in res:
            #self.label_Ergebnis_Verluste_Text.setText(f"{res['loss_total_percent']:.1f} %")
            sys_loss = res_all[0].get("user_system_loss_pct", 0.0)
            tmp_loss = res_all[0].get("internal_losses_pct", {}).get("TempLow", 0.0)
            opt_loss = res_all[0].get("internal_losses_pct", {}).get("OptWR", 0.0)

        # --- 1) konsolenfreundliche Ausgabe ---------------------------
        # print("\n" + "=" * 60)
        # print("BKWSimX  –  Ergebnis der Berechnung")
        # print("=" * 60)
        
        # --------------------------------------------------------------
        # Labels im Ergebnis-Tab – Durchschnittswert Jahres-Wirkungsgrad
        # --------------------------------------------------------------
        self._has_result = True

        # -------------------------------------------------------------
        #  Speicher-Optimierung anzeigen
        # -------------------------------------------------------------
        disabled = []
        for u in self._scenario_units:
            if u and res_all[u].get("disabled_months"):
                disabled = res_all[u]["disabled_months"]
                break

        self.label_Ergebnis_Speicheropt.setText(_disabled_label(disabled))

        # Diagramm zeichnen (disabled-Monate übergeben)
        self._plot_result_chart(res_all, disabled_months=disabled)

        # ––– Tabelle befüllen ––––––––––––––––––––––––––––––––––––––––
        rows = []

        def _merge_rows(key: str) -> list[tuple[str, list[str]]]:
            labels = [r[0] for r in next(iter(res_all.values()))[key]]
            merged = []
            for idx, lbl in enumerate(labels):
                vals = []
                for units in self._scenario_units:
                    row = res_all[units][key][idx]
                    vals.append(row[1] if units == 0 else row[2])
                merged.append((lbl, vals))
            return merged

        # Abschnitts-Überschriften als Leerzeilen einfügen
        sections = [
            ("Ertrag & Nutzen",     _merge_rows("rows_gain")),
            ("Kosten & Wirtschaft", _merge_rows("rows_econ")),
            ("Umwelt-Kennzahlen",   _merge_rows("rows_env")),
            ("Speicher",            _merge_rows("rows_bat")),
            ("Wirkungsgrade",       _merge_rows("rows_efficiency")),
            ("Verluste",            _merge_rows("rows_loss")),
        ]

        self._model_res.setRowCount(0)
        bold = QFont(); bold.setBold(True)

        for title, block in sections:
            # Überschrift
            hdr = [QStandardItem(title)]
            hdr[0].setFont(bold)
            self._model_res.appendRow(hdr)
            # Datenzeilen
            for label, values in block:
                self._model_res.appendRow(
                    [QStandardItem(label)] + [QStandardItem(v) for v in values]
                )
            # Leerzeile zur optischen Trennung
            self._model_res.appendRow([QStandardItem("")])

        # Spaltenbreite
        self.tableView_Ergebnis_Tabelle.resizeColumnsToContents()

        # Diagramm + Tab-Umschaltung / Button-Enable unverändert
        #self._plot_result_chart(res_all)
        self.tabWidget.setCurrentWidget(self.tab_ergebnis)
        self._btn_calc.setEnabled(True)

        # ----- 3) Reiter umschalten & Button frei geben -------------------
        self.tabWidget.setCurrentWidget(self.tab_ergebnis)
        self._btn_calc.setEnabled(True)

    # ------------------------------------------------------------------
    # Fehler‑Callback  – wird vom CalcWorker ausgesendet
    # ------------------------------------------------------------------
    @QtCore.pyqtSlot(str)
    def _on_error(self, msg: str) -> None:
        """Zeigt eine Fehlermeldung und aktiviert den Berechnen‑Button."""
        # Dialog schließen
        self._wait_dialog.hide()
        QtWidgets.QMessageBox.critical(self, "Berechnungsfehler", msg)
        self._btn_calc.setEnabled(True)

    # -------------------------------------------------------------
    #   Ergebnis‑Tab – Modelle & Charts
    # -------------------------------------------------------------
    def _init_results_tab(self) -> None:
        """wird nur EINMAL beim Start aufgerufen."""
        self._models_ready = False  # noch keine Szenario-Spalten erzeugt

        # Matplotlib-Canvas aufbauen
        self._fig = Figure(figsize=(6, 3))
        self._canvas = FigureCanvas(self._fig)
        lay = QtWidgets.QVBoxLayout(self.frame_Ergebnis_Diagramm)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._canvas)
        
        # —————— Legende bauen ——————
        # Frame, das Du im Designer angelegt hast
        legend_frame = self.frame_Ergebnis_Diagramm_Legende
        # Horizontal-Layout für die Legende
        legend_layout = QtWidgets.QHBoxLayout(legend_frame)
        legend_layout.setContentsMargins(10, 0, 0, 0)
        legend_layout.setSpacing(12)

        # Definition der Einträge: (Text, Farbe)
        legend_items = [
            ("PV-Erzeugung", "#F5BD60"),
            ("Eigenverbrauch", "#84A59E"),
            ("Einspeisung",   "#C9C7D1"),
        ]
        for text, col in legend_items:
            # Farbrechteck
            swatch = QtWidgets.QLabel(legend_frame)
            swatch.setFixedSize(9, 9)
            swatch.setStyleSheet(f"background-color: {col}; border: 1px solid #333;")
            # Beschriftung
            lbl = QtWidgets.QLabel(text, legend_frame)
            lbl.setContentsMargins(4, 0, 12, 0)
            # zum Layout hinzufügen
            legend_layout.addWidget(swatch)
            legend_layout.addWidget(lbl)

        # Restlichen Platz füllen
        legend_layout.addStretch()

        # Ergebnis-Tabelle vorbereiten
        view = self.tableView_Ergebnis_Tabelle
        self._model_res = QStandardItemModel(self)
        view.setModel(self._model_res)
        hh = view.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

        # **Initiale Befüllung der Labels** via _update_system_overview
        # so hast du kein doppelten Code und keine Platzhalter mehr
        self._update_system_overview()

    def _build_result_model(self, n_scenarios: int) -> None:
        """
        Spalten der Ergebnis-Tabelle setzen + Header-Resize-Modi so wählen,
        dass …

        • Spalte 0  („Kennzahl“)   ⇒  Breite nach Inhalt
        • alle restlichen Spalten   ⇒  teilen sich den übrigen Platz gleichmäßig
        """
        headers = ["Kennzahl"] + [
            "ohne Speicher" if i == 0 else f"{i} Speicher"
            for i in range(n_scenarios)
        ]
        self._model_res.setColumnCount(len(headers))
        self._model_res.setHorizontalHeaderLabels(headers)

        hh = self.tableView_Ergebnis_Tabelle.horizontalHeader()
        # erst alles auf Stretch stellen …
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        # … dann Spalte 0 auf „nach Inhalt“
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)

    # ------------------------------------------------------------------
    #  Tabellen füllen & Spaltenbreite anpassen
    # ------------------------------------------------------------------
    def _fill_table(
            self,
            view: QtWidgets.QTableView,
            model: QStandardItemModel,
            rows: list[tuple[str, list[str]]],
    ) -> None:
        """Schreibt *rows* in *model* und passt die Spalten im *view* an."""
        model.setRowCount(0)
        for name, values in rows:
            model.appendRow([QStandardItem(name)] +
                            [QStandardItem(v) for v in values])

        view.resizeColumnsToContents()
        view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )

    # ------------------------------------------------------------------
    #  Diagramm zeichnen – saubere Offsets & Legende
    # ------------------------------------------------------------------
    def _plot_result_chart(self, res_all: dict[int, dict], *,
                       disabled_months: list[int] | None = None) -> None:
        self._fig.clear()
        ax = self._fig.add_subplot(111)

        months = range(1, 13)
        n_scen = len(self._scenario_units)

        # ---------------- Offsets & Balkenbreite ------------------------
        #   Ziel: Jede Monats‑Gruppe passt sicher in ±0.45 Einheiten
        #         (damit bleibt links/rechts Luft) – egal wie viele Szenarien.
        #
        gap = 0.05                                # fester Daten‑Abstand ≈ 2 %
        max_span = 0.90                           # ges. Breite pro Monat
        bw = (max_span - len(self._scenario_units) * gap) / (len(self._scenario_units) + 1)
        bw = max(0.08, min(0.22, bw))             # harte Grenzen 0.08 … 0.22

        span = len(self._scenario_units) * bw + (len(self._scenario_units)-1) * gap
        pv_off = -span / 2 - gap - bw             # PV‑Bar immer links außerhalb

        def _off(idx: int) -> float:              # Offset je Szenario‑Index
            return -span/2 + idx * (bw + gap)


        colors = dict(PV="#F5BD60", use="#84A59E", sur="#C9C7D1")

        disabled_months = disabled_months or []

        # ---------------- PV‑Balken (einmal) ---------------------------
        ax.bar([m + pv_off for m in months], res_all[0]["mon_prod"],
               width=bw, color=colors["PV"], edgecolor="#333",
               label="PV‑Erzeugung", zorder=3)

        # ---------------- Szenarien-Schleife ---------------------------
        for idx, units in enumerate(self._scenario_units):
            o = _off(idx)
            dat = res_all[units]

            # 1) Rohdaten ziehen (Series oder None)
            if units:
                ser_use = dat.get("mon_use_st")
                ser_sur = dat.get("mon_sur_st")
            else:
                ser_use = dat.get("mon_use_no_st")
                ser_sur = dat.get("mon_sur_no_st")

            # 2) None-Fallback: bei None → Nullenliste
            if ser_use is None:
                mon_use = [0] * len(months)
            else:
                # pandas.Series → Python-Liste
                mon_use = ser_use.tolist() if hasattr(ser_use, "tolist") else list(ser_use)

            if ser_sur is None:
                mon_sur = [0] * len(months)
            else:
                mon_sur = ser_sur.tolist() if hasattr(ser_sur, "tolist") else list(ser_sur)

            # 3) Labels
            lbl_use = "Eigenverbrauch" + ("" if units == 0 else f" ({units} S)")
            lbl_sur = "Einspeisung"    + ("" if units == 0 else f" ({units} S)")

            # 4) Balken zeichnen
            bc_use = ax.bar([m + o for m in months],
                            mon_use,
                            width=bw,
                            color=colors["use"],
                            alpha=.9,
                            label=lbl_use)
            bc_sur = ax.bar([m + o for m in months],
                            mon_sur,
                            width=bw,
                            bottom=mon_use,
                            color=colors["sur"],
                            alpha=.8,
                            label=lbl_sur)

            # 5) Rote Kontur für „abgeschaltete“ Monate
            if units and disabled_months:
                for patch, mon in zip(bc_use.patches, months):
                    if mon in disabled_months:
                        patch.set_edgecolor("red")
                        patch.set_linewidth(1.4)
                for patch, mon in zip(bc_sur.patches, months):
                    if mon in disabled_months:
                        patch.set_edgecolor("red")
                        patch.set_linewidth(1.4)

        # ---------------- Achsen & Grid -------------------------------
        ax.set_xticks(months, ["Jan","Feb","Mär","Apr","Mai","Jun",
                               "Jul","Aug","Sep","Okt","Nov","Dez"],
                      rotation=45)
        # Y‑Achsen­label dichter an die Achse  → kleineres labelpad
        ax.set_ylabel("kWh", labelpad=4)
        ax.set_ylim(0, ax.get_ylim()[1]*1)
        ax.grid(axis="y", linestyle="--", alpha=.6)

        # ---------------- Mouse‑Over‑Tooltip --------------------------
        cursor = mplcursors.cursor([c for c in ax.containers], hover=True)
        @cursor.connect("add")
        def _on_add(sel):
            bar = sel.artist
            val = bar.datavalues[sel.index]           # Wert aus dem Container holen
            sel.annotation.set_text(f"{bar.get_label()}\n{val:.0f} kWh")    # Tooltip
            sel.annotation.get_bbox_patch().set(fc="white", alpha=.9)       # Hintergrund
            sel.annotation.arrow_patch.set_visible(False)                   # kein Pfeil

        # Rand­abstände manuell justieren: links enger, unten etwas Platz für xticks
        self._fig.subplots_adjust(left=0.08,   # 8 %   statt ~12 %
                                right=0.99,
                                top=0.97,
                                bottom=0.18) # falls xtick‑Labels zweizeilig
        
        self._fig.tight_layout()
        self._canvas.draw_idle()
        
    def _save_project(self) -> None:
        """Speichert das aktuelle Projekt an den bekannten Pfad oder fordert 'Speichern unter...' an."""
        if self._current_project_path:
            self._save_project_to_file(self._current_project_path)
        else:
            self._save_project_as()

    def _save_project_as(self) -> None:
        """Öffnet den Dateidialog 'Speichern unter...'."""
        fname, _ = QFileDialog.getSaveFileName(self,
            "Projekt speichern", "",
            "JSON-Dateien (*.json);;Alle Dateien (*)")
        if fname:
            self._current_project_path = fname
            self._save_project_to_file(fname)

    def _save_project_to_file(self, path: str) -> None:
        """Schreibt die GUI-Parameter als JSON in die Datei."""
        settings = self._collect_settings()
        data = asdict(settings)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _open_project(self) -> None:
        """Öffnet einen bestehenden Projekt-File und lädt die Parameter."""
        fname, _ = QFileDialog.getOpenFileName(self,
            "Projekt öffnen", "",
            "JSON-Dateien (*.json);;Alle Dateien (*)")
        if not fname:
            return
        with open(fname, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # mppts als Dicts → in GeneratorConfig-Objekte umwandeln
        if "mppts" in data:
            data["mppts"] = [
                GeneratorConfig(**mp) for mp in data["mppts"]
            ]
        # alle anderen Felder bleiben unverändert
        settings = Settings(**data)
        self._apply_settings(settings)
        self._current_project_path = fname

    def _apply_settings(self, settings: Settings) -> None:
        """Überträgt alle Felder aus `settings` zurück in die GUI."""
        # 1) Standort
        self.doubleSpinBox_Standort_Breitengrad.setValue(settings.latitude)
        self.doubleSpinBox_Standort_Laengengrad.setValue(settings.longitude)
        
        # 1a) Hersteller aus gespeichertem Feld setzen
        if settings.manufacturer:
            idx_man = self.comboBox_System_Hersteller.findText(settings.manufacturer)
            if idx_man >= 0:
                # Combo auf gespeicherten Hersteller setzen
                self.comboBox_System_Hersteller.setCurrentIndex(idx_man)
                # ruft _on_manufacturer_change() auf und füllt
                # comboBox_System_PV_System korrekt neu

        # 2) System-Auswahl anhand von UserData oder sichtbarem Text
        idx_sys = self.comboBox_System_PV_System.findData(settings.system_name)
        if idx_sys < 0:
            idx_sys = self.comboBox_System_PV_System.findText(settings.system_name)
        if idx_sys >= 0:
            self.comboBox_System_PV_System.setCurrentIndex(idx_sys)
            # Aktualisiert Inverter- und Batterie-Listen
            self._on_system_change()

        # 3) Wechselrichter-Auswahl
        if settings.inverter_model:
            idx_inv = self.comboBox_System_Inverter.findText(settings.inverter_model)
            if idx_inv >= 0:
                self.comboBox_System_Inverter.setCurrentIndex(idx_inv)

        # 4) Batterie-Auswahl
        if settings.battery_model:
            idx_bat = self.comboBox_System_Speichertyp.findText(settings.battery_model)
            if idx_bat >= 0:
                self.comboBox_System_Speichertyp.setCurrentIndex(idx_bat)

        # 5) Speicher-Konfiguration
        self.spinBox_System_Speichermodule.setValue(settings.batt_units)
        self.spinBox_System_SOC_min.setValue(int(settings.soc_min_pct))
        self.spinBox_System_SOC_max.setValue(int(settings.soc_max_pct))

        # 6) Profil
        self.spinBox_Jahesverbrauch.setValue(int(settings.annual_load_kwh))
        self.radioButton_Profil_Berufstaetig.setChecked(settings.profile == "worker")
        self.radioButton_Profil_Rentner.setChecked(settings.profile == "retiree")

        # 7) Generator-Seiten neu aufbauen
        #   zuerst vorhandene Seiten entfernen
        for i in reversed(range(self.stackedWidget_Generator.count())):
            page = self.stackedWidget_Generator.widget(i)
            self.stackedWidget_Generator.removeWidget(page)
        self.listWidget_Generator.clear()
        #   dann anhand settings.mppts wieder hinzufügen
        for mppt in settings.mppts:
            self._add_generator_page()
            page = self.stackedWidget_Generator.currentWidget()
            page.findChild(QtWidgets.QSpinBox,   "spinBox_Modulanzahl_Generator_").setValue(mppt.n_modules)
            page.findChild(QtWidgets.QComboBox,  "comboBox_Verschaltung_Generator_").setCurrentText(
                "direkt" if mppt.connection == "direct" else "reihe"
            )
            page.findChild(QtWidgets.QSpinBox,   "spinBox_Leistung_Generator_").setValue(int(mppt.wp_module))
            page.findChild(QtWidgets.QSpinBox,   "spinBox_Neigung_Generator_").setValue(int(mppt.tilt_deg))
            page.findChild(QtWidgets.QSpinBox,   "spinBox_Azimut_Generator_").setValue(int(mppt.azimuth_deg))
            page.findChild(QtWidgets.QComboBox,  "comboBox_MPPT_Input").setCurrentIndex(mppt.mppt_index - 1)
            # 1) Schattierungsmodus (Radio-Buttons) setzen
            rb_simple  = page.findChild(QtWidgets.QRadioButton, "radioButton_Shade_Simple")
            rb_monthly = page.findChild(QtWidgets.QRadioButton, "radioButton_Shade_Monthly")
            rb_simple .setChecked(mppt.shading_mode == "einfach")
            rb_monthly.setChecked(mppt.shading_mode == "monatlich")

            # 2) Combo Level & Toggle aktivieren/deaktivieren
            page.findChild(QtWidgets.QComboBox, "comboBox_Shade_Level")\
                .setCurrentText(mppt.shading_simple_lvl)
            # aktualisiert die Spinboxes entsprechend dem gewählten Modus
            self._toggle_shade_mode(page)

            # 3) Monatliche Werte befüllen
            for m, pct in mppt.shading_monthly_pct.items():
                name = f"spinBox_Verschattung_{_month_abbr(m)}"
                page.findChild(QtWidgets.QSpinBox, name).setValue(int(pct))

            self._update_system_overview()

        # 8) Verluste
        # ─────────────────────────────────────────────────────────────
        # Ursprüngliche Daten (evtl. mit unicode-Bindestrichen)
        raw_losses = settings.losses_pct or {}

        # 8a) Key-Normalisierung: alle unicode-Bindestriche → ASCII '-'
        losses: dict[str, float] = {}
        for k, v in raw_losses.items():
            # k könnte z.B. "Modul-Mismatch" (\u2011) oder "Modul-Mismatch" sein
            norm = k.replace("\u2011", "-").replace("\u2010", "-")
            losses[norm] = v

        # 8b) Jetzt die Widgets füllen – Labels benutzen ASCII-Bindestrich
        for label, widget_name in (
            ("Leitungsverluste",         "doubleSpinBox_Verluste_Leitungsverluste"),
            ("Verschmutzung",            "doubleSpinBox_Verluste_Verschmutzung"),
            ("Modul-Mismatch",           "doubleSpinBox_Verluste_Modul_Mismatch"),
            ("LID",                      "doubleSpinBox_Verluste_LID"),
            ("Nameplate-Toleranz",       "doubleSpinBox_Verluste_Nameplate_Toleranz"),
            ("Alterung",                 "doubleSpinBox_Verluste_Alterung"),
        ):
            sb = self.findChild(QtWidgets.QDoubleSpinBox, widget_name)
            # sichere Lookup-Reihenfolge: normiertes Label, ansonsten 0.0
            val = losses.get(label, 0.0)
            sb.setValue(val)

        # 9) Kosten
        self.doubleSpinBox_Kosten_Modulkosten.setValue(settings.cost_module_eur)
        self.doubleSpinBox_Kosten_Wechselrichter.setValue(settings.cost_inverter_eur)
        self.doubleSpinBox_Kosten_Installationskosten.setValue(settings.cost_install_eur)
        self.doubleSpinBox_Kosten_Speicherkosten.setValue(settings.cost_battery_eur)
        self.doubleSpinBox_Kosten_Foerderung.setValue(settings.subsidy_eur)
        self.doubleSpinBox_Kosten_Strompreis.setValue(settings.price_eur_per_kwh)
        self.doubleSpinBox_Kosten_Strompreissteigerung.setValue(settings.price_escalation_pct)
        self.doubleSpinBox_Kosten_Betriebszeit.setValue(settings.operating_years)
        self.doubleSpinBox_Kosten_CO2.setValue(settings.co2_factor)
        self._update_cost_display()

    def _toggle_debug_window(self, checked: bool) -> None:
        """Slot für checkBox_debug_window: Debug-Fenster zeigen/verstecken."""
        if checked:
            self._debug_window.show()
        else:
            self._debug_window.hide()