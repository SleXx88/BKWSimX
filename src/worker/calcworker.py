# worker/calcworker.py

from __future__ import annotations
from dataclasses import replace
from PyQt6 import QtCore
import traceback
import time
import logging

from logic.calculation import Settings, run_calculation


class CalcWorker(QtCore.QThread):
    finished = QtCore.pyqtSignal(dict)     # {units: result_dict, …}
    error    = QtCore.pyqtSignal(str)

    def __init__(self, base_settings: Settings, units_list: list[int]):
        super().__init__()
        self._settings   = base_settings
        self._units_list = units_list

    def run(self):
        import logic.calculation as calc_mod
        logger = logging.getLogger("logic.calculation")
        try:
            # Gesamtlauf ohne SCN-Prefix:
            calc_mod.CURRENT_SCENARIO = None
            logger.debug("=== Berechnung gestartet ===")
            start_all = time.perf_counter()

            results: dict[int, dict] = {}
            for units in self._units_list:
                # CURRENT_SCENARIO für den Filter setzen
                # Szenario-Start mit [STRT]
                calc_mod.CURRENT_SCENARIO = units
                logger.debug("[START] Szenario %d gestartet (Einh=%d)", units, units)
                start = time.perf_counter()

                sett = replace(self._settings, batt_units=units)
                results[units] = run_calculation(sett)

                elapsed = (time.perf_counter() - start) * 1000
                # Szenario-Ende mit [END]
                logger.debug("[END  ] Szenario %d beendet in %.0f ms", units, elapsed)

            total = (time.perf_counter() - start_all) * 1000
            # Nach Abschluss kein SCN-Prefix mehr:
            calc_mod.CURRENT_SCENARIO = None
            logger.debug("=== Berechnung abgeschlossen in %.0f ms ===", total)
            calc_mod.CURRENT_SCENARIO = None

            self.finished.emit(results)
        except Exception as exc:
            tb = "".join(traceback.format_exception(exc, value=exc, tb=exc.__traceback__))
            self.error.emit(tb)
