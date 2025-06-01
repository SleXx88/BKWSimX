# BKWSimX – Simulation von PV-Systemen
# logic.calculation
# Kernfunktion für die Simulation von PV-Systemen.
from __future__ import annotations

import calendar
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pvlib 
from pvlib.iotools import get_pvgis_hourly
from pvlib import atmosphere, irradiance, iam

from bkwsimx import __version__

# ---------------------------------------------------------------------------
# Kompaktes Logging-Format + automatische Szenario-ID
# ---------------------------------------------------------------------------
import itertools
import logging

_scn_counter     = itertools.count()
_original_factory = logging.getLogRecordFactory()
_current_scn      = 0

def _scn_factory(*a, **kw):
    rec, rec.scn = _original_factory(*a, **kw), _current_scn
    return rec

logging.setLogRecordFactory(_scn_factory)

fmt = logging.Formatter("%(levelname).1s [SCN%(scn)d] %(message)s")

# ---------- Root-Logger: NUR Infos/Warnungen aus Bibliotheken ---------------
root = logging.getLogger()
root.setLevel(logging.INFO)              ### hier von DEBUG → INFO/WARNING

if not root.handlers:                     # Fallback-Handler
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(fmt)
    root.addHandler(h)
for h in root.handlers:                   # Format vereinheitlichen
    h.setFormatter(fmt)

# ---------- Dein Modul-Logger ----------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)            # eigene DEBUG-Ausgaben
logger.propagate = False                  ### verhindert Doppel-Logging

# eigener Handler nur für dieses Modul
_mod_handler = logging.StreamHandler(sys.stdout)
_mod_handler.setFormatter(fmt)
_mod_handler.setLevel(logging.DEBUG)
logger.addHandler(_mod_handler)

# ---------------------------------------------------------------------------
# Hilfsfunktion – bei jedem run_calculation() einmal aufrufen
# ---------------------------------------------------------------------------
def _new_scenario():
    global _current_scn
    _current_scn = next(_scn_counter)

# ---------------------------------------------------------------------------
# Hilfsfunktionen & Konstanten
# ---------------------------------------------------------------------------

### NEW BEGIN ###  – Zusatz‑Konstanten für Feintakt‑Modelle
FALLBACK_TIMESTEP_MIN = 60        # alte Logik
DEFAULT_TIMESTEP_MIN  = 15        # neuer Standard
FORECAST_CUTOFF_HOUR  = 14        # bis dahin nur auf 80 % SoC laden
SOC_TARGET_AM      = 0.80         # 80 %
### NEW END ###

def _resource_path(fname: str) -> str:
    """Unterstützt PyInstaller‑Bundle (OFFICIAL) und Dev‑Umgebung."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, fname)  # type: ignore[attr-defined]
    return os.path.join(Path(__file__).resolve().parent.parent, fname)

# Verbrauchsprofile (identisch zum ursprünglichen Tk‑Code)
_daily_ret = np.array([
    0.04, 0.04, 0.04, 0.04, 0.05, 0.06, 0.07, 0.08, 0.10, 0.10, 0.10,
    0.10, 0.10, 0.08, 0.07, 0.06, 0.05, 0.05, 0.04, 0.04, 0.04, 0.04,
    0.04, 0.04,
])
_daily_work = np.array([
    0.04, 0.04, 0.04, 0.04, 0.06, 0.08, 0.10, 0.10, 0.08, 0.06, 0.04,
    0.04, 0.04, 0.04, 0.04, 0.04, 0.06, 0.08, 0.10, 0.10, 0.08, 0.06,
    0.04, 0.04,
])
_daily_ret  /= _daily_ret.sum()
_daily_work /= _daily_work.sum()
_monthly_w = np.array([
    0.106, 0.096, 0.087, 0.076, 0.063, 0.053, 0.054, 0.062, 0.072,
    0.091, 0.100, 0.114,
])
_monthly_w /= _monthly_w.sum()

# ---------------------------------------------------------------------------
# Einheitliche Debug-Ausgabe im Tabellendesign
# ---------------------------------------------------------------------------
_SECTION_WIDTH = 5  # jetzt genau 5 Zeichen im Label

def dbg(section: str, fmt: str, *args):
    """
    Einheitliche Debug-Ausgabe:
    - section: Kurzlabel (3–5 Zeichen), wird zu 5 Zeichen gepaddet
    - fmt: Format-String mit {}-Platzhaltern
    """
    sec = section.upper().ljust(_SECTION_WIDTH)   # pad auf 5
    text = fmt.format(*args)
    logger.debug(f"[{sec}] {text}")

# ---------------------------------------------------------------------------
# Einfache Format-Helper  → sorgen für einheitliche Zahlen­darstellung
# ---------------------------------------------------------------------------
def fmt0(val: float | int) -> str:
    """Ganzzahlig, 1000er-Leerzeichen (1 278)"""
    return f"{val:,.0f}".replace(",", " ")

def fmt1(val: float | int) -> str:
    """1 Nachkommastelle (86.6) – 1000er-Leerzeichen"""
    return f"{val:,.1f}".replace(",", " ")

def fmt2(val: float | int) -> str:
    """2 Nachkommastellen (23.89) – 1000er-Leerzeichen"""
    return f"{val:,.2f}".replace(",", " ")

def pct1(val: float) -> str:
    """Prozent mit 1 Nachkommastelle (86.6 %)"""
    return f"{fmt1(val)} %"

# ---------------------------------------------------------------------------
# Dataclasses – Eingaben
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class GeneratorConfig:
    """Parameter eines PV‑Generators (= ein Modul oder mehrere Module in Serie)."""
    mppt_index:   int       # 1‑basiert – zu welchem MPPT gehört der Generator?
    n_modules:    int
    connection:   str       # "direct" | "series"
    wp_module:    float     # Wp je Einzel­modul
    tilt_deg:     float     # Modul­neigung (°)
    azimuth_deg:  float     # Azimut 0 = Süd, −90 = Ost, 90 = West
    # Schattierung pro Generator (NEU)
    shading_mode:        str   = "einfach"   # "einfach" | "monatlich"
    shading_simple_lvl:  str   = "keine"
    shading_monthly_pct: Dict[int, float] = field(default_factory=lambda: {m: 0.0 for m in range(1,13)})

@dataclass(slots=True)
class Settings:
    """Sämtliche Simulationseingaben in einem Objekt."""
    # **A. Standort & Zeitraum**
    latitude: float
    longitude: float
    manufacturer: str
    system_name: str
    years: Tuple[int, int] = (2020, 2023)      # (2016, 2022)   
    timestep_min: int = DEFAULT_TIMESTEP_MIN   # 60 ⇒ altes 1‑h‑Raster

    # **B. Hardware**
    inverter_model: Optional[str] = None
    battery_model: Optional[str] = None
    batt_units: int = 0

    soc_min_pct: float = 10.0
    soc_max_pct: float = 100.0

    # **C. Kosten‑Parameter**
    cost_module_eur: float = 70.0
    cost_inverter_eur: float = 249.0
    cost_install_eur: float = 80.0
    cost_battery_eur: float = 599.0
    subsidy_eur: float = 300.0

    price_eur_per_kwh: float = 0.32
    price_escalation_pct: float = 1.5
    operating_years: int = 15
    co2_factor: float = 0.281

    # **D. Verluste & Verschattung**
    losses_pct: Dict[str, float] = field(default_factory=lambda: {
        "Leitungsverluste": 2,
        "Verschmutzung": 2,
        "Modul‑Mismatch": 2,
        "LID": 1,
        "Nameplate‑Toleranz": 3,
        "Alterung": 2,
    })

    shading_mode: str = "einfach"  # "einfach" | "monatlich"
    shading_simple_level: str = "keine"
    shading_monthly_pct: Dict[int, float] = field(default_factory=lambda: {m: 0.0 for m in range(1, 13)})

    # **E. Verbrauch**
    annual_load_kwh: float = 3000.0
    profile: str = "retiree"       # "retiree" | "worker"

    # NEU: soll das Modell den Speicher-Nutzen selbst optimieren?
    optimize_storage: bool = False          # ← Default: aus
    
    mppts: List[GeneratorConfig] = field(default_factory=list)

# ---------------------------------------------------------------------------
# Datenbanken laden
# ---------------------------------------------------------------------------

with open(_resource_path("resources\\pv_systems.json"), "r", encoding="utf-8") as f:
    _pv_systems = json.load(f)
with open(_resource_path("resources\\inverters.json"), "r", encoding="utf-8") as f:
    _inverters = json.load(f)
with open(_resource_path("resources\\batteries.json"), "r", encoding="utf-8") as f:
    _batteries = json.load(f)

_sys_by_name   = {s["name"]: s for s in _pv_systems}
_inv_by_model  = {i["model"]: i for i in _inverters}
_batt_by_model = {b["model"]: b for b in _batteries}
_inv_by_id     = {i["id"]:    i for i in _inverters}
_batt_by_id    = {b["id"]:    b for b in _batteries}

# ---------------------------------------------------------------------------
# Hilfsroutinen
# ---------------------------------------------------------------------------

def _escalated_cashflow(kwh: float, price: float, esc_pct: float, years: int) -> float:
    """Barwert einer kWh‑Ersparnis mit jährlicher Preissteigerung."""
    esc = esc_pct / 100.0
    if esc == 0:
        return kwh * price * years
    return kwh * price * ((1 + esc) ** years - 1) / esc

def _interpolate_weather(df: pd.DataFrame, dt_min: int) -> pd.DataFrame:
    """Bringt PVGIS‑Stundenwerte per linearem Interpolieren auf *dt_min*."""
    if dt_min >= 60:
        return df                        # nichts zu tun
    new_idx = pd.date_range(
        start=df.index[0], end=df.index[-1],
        freq=f"{dt_min}min", tz=df.index.tz
    )
    return (
        df.reindex(df.index.union(new_idx))
          .interpolate(method="time")
          .reindex(new_idx)
    )
# ---------------------------------------------------------------------------
#   PVGIS-Cache  –  vermeidet Mehrfach-Downloads pro Berechnung
# ---------------------------------------------------------------------------
_PVGIS_CACHE: dict[tuple, pd.DataFrame] = {}

def _get_pvgis_cached(latitude: float, longitude: float,
                      start_year: int, end_year: int,
                      tilt: float, azimuth: float) -> pd.DataFrame:
    """
    Holt PVGIS-SARAH3-Daten *einmal* pro Standort / Jahr / Modul­neigung.

    • Beim ersten Aufruf -> HTTP-Request, Ergebnis wird im Modul-Cache
      abgelegt.
    • Danach -> `.copy()` des gecachten DataFrames (thread-sicher und
      ohne Seiteneffekte).

    Der Key wird grob gerundet, damit „dieselbe“ Eingabe nicht durch
    Mikro-Abweichungen doppelt im Cache landet.
    """
    key = (round(latitude, 4), round(longitude, 4),
           start_year, end_year,
           round(tilt, 1), round(azimuth, 1))

    if key not in _PVGIS_CACHE:
        #logger.debug("PVGIS: lade Wetterdaten neu für %s", key)
        df, *_ = get_pvgis_hourly(
            latitude=latitude, longitude=longitude,
            start=start_year, end=end_year,
            map_variables=True,
            surface_tilt=tilt, surface_azimuth=azimuth,
            url="https://re.jrc.ec.europa.eu/api/v5_3/",
            raddatabase="PVGIS-SARAH3",
        )
        _PVGIS_CACHE[key] = df
    # else:
    #     #logger.debug("PVGIS: benutze Cache (%s)", key)

    return _PVGIS_CACHE[key].copy()            # niemals das Original ändern!

# ---------------------------------------------------------------------------
# Kernfunktion
# ---------------------------------------------------------------------------
def run_calculation(settings: Settings, *, progress: Optional[Callable[[int], None]] = None) -> Dict[str, object]:
    """Führt die komplette Simulation aus und liefert ein Ergebnis‑Dict.
    
    Args:
        settings:  Alle Eingabedaten.
        progress:  Optionaler Callback (0‑100 %).
    """
    _new_scenario()
    dt_h = settings.timestep_min / 60.0        # Stunden pro Zeitschritt (z. B. 0.25 h)

    def _report(pct: int):
        if progress:
            progress(pct)

    lat, lon = settings.latitude, settings.longitude
    start_year, end_year = settings.years
    n_years = end_year - start_year + 1

    sys_obj = _sys_by_name.get(settings.system_name, _pv_systems[0])
    inv_obj = _inv_by_model.get(settings.inverter_model) if settings.inverter_model else None
    batt_obj = _batt_by_model.get(settings.battery_model) if settings.battery_model else None
    sys_type = sys_obj.get("type", "hybrid").lower()
    
    # System-Ausgabe (Hersteller, Modell, Typ, Speicher)
    dbg("SYSTM", "System: Hersteller={}  Modell={}  Speicher={}",
        sys_obj.get("manufacturer", "unbekannt"),
        sys_obj.get("name", "unbekannt"),
        batt_obj["model"] if batt_obj and settings.batt_units else "kein Speicher",
    )

    # ------------------------------------------------------------------
    # 1) Wetterdaten abrufen  –  jetzt mit Cache
    # ------------------------------------------------------------------
    _report(5)
    try:
        df_weather = _get_pvgis_cached(
            latitude=lat, longitude=lon,
            start_year=start_year, end_year=end_year,
            tilt=settings.mppts[0].tilt_deg if settings.mppts else 35,
            azimuth=settings.mppts[0].azimuth_deg if settings.mppts else 0,
        )
    except Exception as exc:
        raise RuntimeError(f"PVGIS-Abruf fehlgeschlagen: {exc}") from exc

    df_weather = df_weather.copy()
    rename = {
        "G(h)": "ghi",
        "Gb(n)": "dni",
        "Gd(h)": "dhi",
        "T2m": "temp_air",
        "Tair": "temp_air",
        "Ta": "temp_air",
        "WS10m"  : "wind_speed",
    }
    df_weather.rename(columns=rename, inplace=True)
    
    df_weather = _interpolate_weather(df_weather, settings.timestep_min)

    # ------------------------------------------------------------
    # sicherstellen, dass ghi/dni/dhi vorhanden sind
    # ------------------------------------------------------------
    if {"ghi", "dni", "dhi"} - set(df_weather.columns):
        poa_cols = {"poa_direct", "poa_sky_diffuse", "poa_ground_diffuse"}
        if poa_cols.issubset(df_weather.columns):
            df_weather["poa_global"] = (
                df_weather["poa_direct"]
                + df_weather["poa_sky_diffuse"]
                + df_weather["poa_ground_diffuse"]
            )
            site_tmp = pvlib.location.Location(lat, lon, tz="UTC")
            sol_tmp  = site_tmp.get_solarposition(df_weather.index)
            cos_zen  = np.cos(np.radians(sol_tmp["zenith"].clip(upper=90)))
            cos_zen  = cos_zen.where(cos_zen > 0, 0)

            df_weather["dni"] = (df_weather["poa_direct"] / cos_zen).replace([np.inf, -np.inf], 0)
            df_weather["dhi"] = df_weather["poa_sky_diffuse"] + df_weather["poa_ground_diffuse"]
            df_weather["ghi"] = df_weather["dni"] * cos_zen + df_weather["dhi"]
        else:
            raise RuntimeError("PVGIS lieferte weder ghi/dni/dhi noch POA‑Komponenten.")

    if {"ghi", "dni", "dhi"} - set(df_weather.columns):
        raise RuntimeError("PVGIS lieferte nicht die erwarteten Strahlungsdaten (ghi/dni/dhi).")

    if "temp_air" not in df_weather.columns:
        df_weather["temp_air"] = 20.0

    if df_weather.index.tz is None:
        df_weather.index = df_weather.index.tz_localize("UTC")
    df_weather = df_weather.tz_convert("Europe/Berlin")

    site = pvlib.location.Location(lat, lon, tz="Europe/Berlin")
    solpos = site.get_solarposition(df_weather.index)
    
    cos_zen = np.cos(np.radians(solpos["zenith"]))
    mask_bad = (cos_zen < 0.01) | (df_weather["dni"] < 0)
    df_weather["dni"] = df_weather["dni"].where(~mask_bad, 0.0)
    
    # Eingangsdaten PVGIS
    dbg("PVGIS", "Wetterdaten: Neigung={}°  Azimut={}°  GHI̅={} W/m²  DNI̅={} W/m²  "
                "T_Luft̅={} °C  ({} Zeilen)",
        settings.mppts[0].tilt_deg, settings.mppts[0].azimuth_deg,
        fmt1(df_weather["ghi"].mean()),
        fmt1(df_weather["dni"].mean()),
        fmt1(df_weather["temp_air"].mean()),
        fmt0(len(df_weather)),
    )

# ------------------------------------------------------------------
# 3) DC-Leistung aller MPPTs
# ------------------------------------------------------------------
    _report(25)
    dbg("MPPTS", "Starte {} MPPT-Berechnungen", len(settings.mppts))

    # Sammel-Variablen (verschatteter Strang)
    total_dc        = 0.0
    dc_ref_total    = 0.0
    dc_25_total     = 0.0
    dc_ideal_total  = 0.0
    direct_fracs: list[pd.Series] = []
    poa_eff_list: list[pd.Series] = []

    # → Liste, damit wir nach der Schleife sauber summieren können
    dc_noshade_list: list[pd.Series] = []

    for mp in settings.mppts:
        # ------------------------------------------------------------------
        # 1) Strahlungs­komponenten kopieren (Basis für beide Rechnungen)
        # ------------------------------------------------------------------
        dni_orig = df_weather["dni"]          # unverändert (Referenz)
        dni_input = dni_orig.copy()           # wird evtl. maskiert
        ghi_input = df_weather["ghi"]
        dhi_input = df_weather["dhi"]

        # ------------------------------------------------------------------
        # 2) Verschattung (einfach/monatlich)  →  nur auf dni_input
        # ------------------------------------------------------------------
        mode = mp.shading_mode.strip().lower()

        if mode == "einfach":
            shade_lvls = {"keine": 0, "leicht": 15, "mittel": 25, "stark": 35}
            thr = shade_lvls.get(mp.shading_simple_lvl.lower(), 0)

            if thr > 0:
                sun_elev = 90 - solpos["zenith"]                       # ° über Horizont
                az_diff  = np.abs((solpos["azimuth"] - mp.azimuth_deg + 180) % 360 - 180)
                mask     = (sun_elev < thr) & (az_diff < 90)           # nur Front-Halbraum
                dni_input = dni_input.where(~mask, 0)

        elif mode == "monatlich":
            pct_map = {m: mp.shading_monthly_pct.get(m, 0) / 100 for m in range(1, 13)}
            pct_arr = pd.Series(df_weather.index.month.map(lambda m: pct_map[m]),
                                index=df_weather.index, dtype=float)
            dni_input *= (1 - pct_arr)

        else:
            raise ValueError(f"Unbekanntes Verschattungs-Modell {mp.shading_mode!r}")

        # ------------------------------------------------------------------
        # 3a)  POA + DC **ohne** Verschattung  (Referenzbasis)
        # ------------------------------------------------------------------
        irr_ref = pvlib.irradiance.get_total_irradiance(
            surface_tilt    = mp.tilt_deg,
            surface_azimuth = mp.azimuth_deg,
            solar_zenith    = solpos["zenith"],
            solar_azimuth   = solpos["azimuth"],
            dni             = dni_orig,     # unmaskiert!
            dhi             = dhi_input,
            ghi             = ghi_input,
        )
        poa_ref      = irr_ref["poa_global"]
        aoi_ref      = irradiance.aoi(mp.tilt_deg, mp.azimuth_deg,
                                      solpos["zenith"], solpos["azimuth"])
        iam_fac_ref  = iam.ashrae(aoi_ref, b=0.035)
        poa_eff_ref  = poa_ref * iam_fac_ref

        dc_noshade_i = pvlib.pvsystem.pvwatts_dc(
            g_poa_effective = poa_eff_ref,
            temp_cell       = pvlib.temperature.faiman(
                poa_global = poa_ref,
                temp_air   = df_weather["temp_air"],
                wind_speed = df_weather.get("wind_speed", 1.0),
                u0 = 20, u1 = 0.0,
            ),
            pdc0      = mp.n_modules * mp.wp_module,
            gamma_pdc = -0.003,
        )
        dc_noshade_list.append(dc_noshade_i)

        # ------------------------------------------------------------------
        # 3b)  POA + DC **mit** Verschattung  (normale Simulation)
        # ------------------------------------------------------------------
        irr = pvlib.irradiance.get_total_irradiance(
            surface_tilt    = mp.tilt_deg,
            surface_azimuth = mp.azimuth_deg,
            solar_zenith    = solpos["zenith"],
            solar_azimuth   = solpos["azimuth"],
            dni             = dni_input,    # maskiert!
            dhi             = dhi_input,
            ghi             = ghi_input,
        )
        poa      = irr["poa_global"]
        aoi      = irradiance.aoi(mp.tilt_deg, mp.azimuth_deg,
                                  solpos["zenith"], solpos["azimuth"])
        iam_fac  = iam.ashrae(aoi, b=0.035)
        poa_eff  = poa * iam_fac
        poa_eff_list.append(poa_eff)

        direct_fracs.append((irr["poa_direct"] / poa).fillna(0))

        # 4) DC-Leistung (verschattet)
        wind = df_weather.get("wind_speed", 1.0)
        dc_i = pvlib.pvsystem.pvwatts_dc(
            g_poa_effective = poa_eff,
            temp_cell       = pvlib.temperature.faiman(
                poa_global = poa,
                temp_air   = df_weather["temp_air"],
                wind_speed = wind,
                u0 = 20, u1 = 0.0,
            ),
            pdc0      = mp.n_modules * mp.wp_module,
            gamma_pdc = -0.003,
        )
        total_dc += dc_i

        # ---------- weitere Referenzgrößen (unverändert) -------------
        dc_ref_i = pvlib.pvsystem.pvwatts_dc(
            g_poa_effective = poa_eff, temp_cell = 25,
            pdc0 = mp.n_modules * mp.wp_module, gamma_pdc = 0.0,
        )
        dc_ref_total += dc_ref_i

        dc_ideal_i = pvlib.pvsystem.pvwatts_dc(
            g_poa_effective = poa, temp_cell = 25,
            pdc0 = mp.n_modules * mp.wp_module, gamma_pdc = 0.0,
        )
        dc_ideal_total += dc_ideal_i

        dc_25_i = pvlib.pvsystem.pvwatts_dc(
            g_poa_effective = poa_eff, temp_cell = 25,
            pdc0 = mp.n_modules * mp.wp_module, gamma_pdc = -0.003,
        )
        dc_25_total += dc_25_i

        # ---------- Debug-Ausgabe ------------------------------------
        dt_h = settings.timestep_min / 60.0
        dc_ref_kwh_year  = dc_ref_i.sum() * dt_h / 1000 / n_years
        dc_real_kwh_year = dc_i.sum()     * dt_h / 1000 / n_years
        pdc_nom_kwp      = mp.n_modules * mp.wp_module / 1000
        y_spec           = dc_real_kwh_year / pdc_nom_kwp

        dbg("MPPTS", "MPPT={}  Neigung={}°  Azimut={}°  POA̅={} W/m²  "
                    "DC_Ref={} kWh/a  DC_Real={} kWh/a  IAM-Verlust={}",
            mp.mppt_index,
            fmt1(mp.tilt_deg),                     # Modulneigung
            fmt1(mp.azimuth_deg),                  # Azimut
            fmt1(poa.mean()),                      # mittlere POA
            fmt1(dc_ref_kwh_year),                 # Referenz-DC (25 °C)
            fmt1(dc_real_kwh_year),                # realer DC-Ertrag
            pct1(100 * (1 - poa_eff.sum() / poa.sum())),
        )

        dbg("MPPTS", "MPPT={}  Leistung={} kWp  Spez_Ertrag={} kWh/kWp·a  "
                    "POA̅={} W/m²  IAM-Verlust={}",
            mp.mppt_index,
            fmt1(pdc_nom_kwp),                     # installierte Leistung
            fmt1(y_spec),                          # spezifischer Ertrag
            fmt1(poa.mean()),                      # mittlere POA
            pct1(100 * (1 - poa_eff.sum() / poa.sum())),
        )

    # ---------------------- Ende for-Schleife --------------------------

    # ---------- Referenz-DC ohne Verschattung zusammenfassen ----------
    dc_noshade_total = sum(dc_noshade_list)

    # ------------------------------------------------------------
    # Sammeln & Maskieren
    # ------------------------------------------------------------
    dc     = total_dc
    dc_ref = dc_ref_total

    if direct_fracs:
        direct_frac = sum(direct_fracs) / len(direct_fracs)
    else:
        direct_frac = pd.Series(0, index=dc.index)

    mask_years = (dc.index.year >= start_year) & (dc.index.year <= end_year)
    dc              = dc.loc[mask_years]
    dc_ref          = dc_ref.loc[mask_years]
    dc_ideal        = dc_ideal_total.loc[mask_years]
    dc_noshade_total = dc_noshade_total.loc[mask_years]

    dc_sum_kwh_year = dc.sum() * dt_h / 1000 / n_years
    dbg("TIME ", "Zeitraum {}–{}  Schritte={}  DC_Gesamt={} kWh/a",
        start_year, end_year,
        fmt0(len(dc)), fmt1(dc_sum_kwh_year),
    )
    _report(35)

    # ------------------------------------------------------------
    # 4a) Hilfs-Interpolator  η(Pdc)   (unverändert)
    # ------------------------------------------------------------
    def _interp_eta(p_dc_w: pd.Series,
                    curve_w: list[int] | None,
                    curve_pct: list[float] | None,
                    eta_fallback: float) -> pd.Series:
        """
        Liefert η(P_dc)  [0…1] via linearer Interpolation der JSON-Kurve.
        Fehlt eine Kurve ⇒ konst. eta_fallback.
        """
        if not curve_w or not curve_pct or len(curve_w) != len(curve_pct):
            return pd.Series(eta_fallback, index=p_dc_w.index)

        w   = np.array(curve_w,   dtype=float)
        pct = np.array(curve_pct, dtype=float)
        if w[0] > 0:                        # 0 W integrieren
            w   = np.insert(w,   0, 0.0)
            pct = np.insert(pct, 0, 0.0)
        if w[-1] < p_dc_w.max():            # rechten Rand abschneiden
            w   = np.append(w,   p_dc_w.max())
            pct = np.append(pct, pct[-1])

        return pd.Series(np.interp(p_dc_w, w, pct/100.0), index=p_dc_w.index)

    # ------------------------------------------------------------
    # 4b)  JSON-Kurve & P_max lesen
    # ------------------------------------------------------------
    eta_sys = (sys_obj.get("ac_efficiency_percent")
           or (inv_obj or {}).get("ac_efficiency_percent", 100)
           or 100) / 100.0

    if sys_obj.get("inverter_integrated"):
        curve_w   = sys_obj.get("efficiency_curve_w")
        curve_pct = sys_obj.get("efficiency_curve_pct")
        max_ac_w  = sys_obj.get("max_ac_output_power_w") or float("inf")
    else:
        curve_w   = inv_obj.get("efficiency_curve_w") if inv_obj else None
        curve_pct = inv_obj.get("efficiency_curve_pct") if inv_obj else None
        max_ac_w  = inv_obj.get("max_output_power_w")  if inv_obj else float("inf")
    
    # Wechselrichter-Grunddaten    
    dbg("INVTR", "WR-Typ={}  P_max={} W  Kennl-Punkte={}",
        sys_type, fmt0(max_ac_w), len(curve_w or []),
    )

    # ------------------------------------------------------------
    # 4c)  Zwei Pfade je Gerätetyp
    # ------------------------------------------------------------
    if sys_type == "charger_only":
        # ------------------------------------------------------
        #   4c-1)  Akku-Simulation auf **DC-Seite**
        # ------------------------------------------------------
        dt_h   = settings.timestep_min / 60.0           # h / Schritt
        n_step = len(dc)

        # --- DC-Verbrauchsprofil (vereinfacht) --------------
        idx_m = dc.index.month - 1
        idx_h = dc.index.hour
        m_w = _monthly_w[idx_m]
        h_w = (_daily_ret if settings.profile == "retiree" else _daily_work)[idx_h]
        q_w = np.ones_like(idx_h) / (60 / settings.timestep_min)
        weights = m_w * h_w * q_w
        weights /= weights.sum()
        load_kwh_dc = settings.annual_load_kwh * weights

        # --- Akku-Parameter ---------------------------------
        batt_cap = 0.0
        if batt_obj and settings.batt_units > 0:
            batt_cap   = batt_obj["capacity_wh"]*settings.batt_units/1000
            standby_kw = batt_obj.get("standby_power_w",0)*settings.batt_units/1000
            eta_rt     = batt_obj.get("roundtrip_efficiency_percent",100)/100
            eta_ch = eta_dis = math.sqrt(eta_rt)
            p_ch_max_kw  = settings.batt_units * batt_obj.get("max_charge_power_w",800)/1000
            p_dis_max_kw = settings.batt_units * batt_obj.get("max_discharge_power_w",1200)/1000
            soc_min = settings.soc_min_pct/100
            soc_max = settings.soc_max_pct/100
        else:
            standby_kw = p_ch_max_kw = p_dis_max_kw = 0.0
            eta_ch = eta_dis = 1.0
            soc_min = soc_max = 0.0

        standby_ts   = standby_kw   * dt_h
        p_ch_max_ts  = p_ch_max_kw  * dt_h
        p_dis_max_ts = p_dis_max_kw * dt_h

        direct_use_dc = np.zeros(n_step)
        batt_out_dc   = np.zeros(n_step)
        idle_dc       = np.zeros(n_step)
        charge_dc     = np.zeros(n_step)
        state_kwh     = 0.0

        for i, (pv_kw, load_kwh) in enumerate(zip(dc.values, load_kwh_dc)):
            pv_kwh = pv_kw * dt_h
            direct = min(pv_kwh, load_kwh)
            direct_use_dc[i] = direct

            surplus = pv_kwh - direct
            deficit = load_kwh - direct

            if batt_cap:
                # Stand-by
                if state_kwh > batt_cap*soc_min:
                    idle = min(state_kwh-batt_cap*soc_min, standby_ts)
                    idle_dc[i] = idle
                    state_kwh -= idle
                # Laden
                if surplus > 0 and state_kwh < batt_cap*soc_max:
                    room = batt_cap*soc_max - state_kwh
                    ch   = min(surplus, p_ch_max_ts, room)
                    charge_dc[i] = ch
                    state_kwh += ch * eta_ch
                    surplus   -= ch
                # Entladen
                if deficit > 0:
                    avail = state_kwh - batt_cap*soc_min
                    di    = min(deficit, p_dis_max_ts, avail)
                    batt_out_dc[i] = di * eta_dis
                    state_kwh -= di

        # PV + Batterie am DC-Bus  (kW)
        dc_bus = (
            pd.Series(dc.values*dt_h, index=dc.index)   # PV
            - pd.Series(charge_dc, index=dc.index)      # Laden
            + pd.Series(batt_out_dc, index=dc.index)    # Entladen
        ) / dt_h

        # ------------------------------------------------------
        #   4c-2)  WR-Kennlinie anwenden
        # ------------------------------------------------------
        eta_inv = _interp_eta(dc_bus, curve_w, curve_pct,
                            eta_fallback=(inv_obj.get("ac_efficiency_percent",100))/100)

        raw_eta = eta_inv.copy()
        #logger.debug(f"RAW Inverter η min/max: {raw_eta.min():.3f}/{raw_eta.max():.3f}")

        eta_inv = eta_inv.clip(upper=1.0)
        #logger.debug(f"CLIPPED Inverter η min/max: {eta_inv.min():.3f}/{eta_inv.max():.3f}")

        ac_raw     = dc_bus * eta_inv
        ac_clipped = ac_raw.clip(upper=max_ac_w)

        # DC/AC-Serien für spätere Auswertungen bereitstellen
        dc = pd.Series(dc_bus, index=dc.index)             # kW nach Akku-Pfad
        batt_out_ac = pd.Series(batt_out_dc*eta_inv*dt_h, index=dc.index)
    else:
        # ------------------------------------------------------
        #   4c-Standardpfad  (hybrid oder reiner WR)
        # ------------------------------------------------------
        eta_inv = _interp_eta(dc, curve_w, curve_pct,
                            eta_fallback=(sys_obj.get("ac_efficiency_percent") or
                                            inv_obj.get("ac_efficiency_percent",100))/100)

        raw_eta = eta_inv.copy()
        #logger.debug(f"RAW Inverter η min/max: {raw_eta.min():.3f}/{raw_eta.max():.3f}")

        eta_inv = eta_inv.clip(upper=1.0)
        #logger.debug(f"CLIPPED Inverter η min/max: {eta_inv.min():.3f}/{eta_inv.max():.3f}")

        ac_raw     = dc * eta_inv
        ac_clipped = ac_raw.clip(upper=max_ac_w)

    # Wechselrichter-Kennlinie
    nz = eta_inv[eta_inv > 0].head(3).round(3).tolist()
    dbg("INVTR", "Kennlinie: η_min={:.1f} %  η_max={:.1f} %  Beispiel η≠0={}",
        eta_inv.min()*100, eta_inv.max()*100, nz)

    # -----------------------------------------------------------------
    # ← NEU: alle negativen Leistungen auf 0 setzen
    # -----------------------------------------------------------------
    dc         = dc.clip(lower=0)
    ac_clipped = ac_clipped.clip(lower=0)

    over = (ac_clipped > dc).sum()           # immer berechnen
    if over:
        ratio = (ac_clipped / dc).nlargest(3)
        
    # Abschneidung
    dbg("INVTR", "Abregelung: {} von {} Schritten gekappt  |  AC<0 korrigiert={}",
        over, len(dc), "ja" if (ac_clipped < 0).any() else "nein"
    )

    # -------------------------------------------------
    #   Energie-gewichtete Wirkungsgrade
    # -------------------------------------------------
    #dt_h = settings.timestep_min / 60.0

    total_dc_kwh      = (dc          * dt_h).sum() / 1000      # real (Temp + γ)
    total_dc_25_kwh   = (dc_25_total * dt_h).sum() / 1000      # nur Low-Irr
    total_dc_ref_kwh  = (dc_ref      * dt_h).sum() / 1000      # STC

    total_ac_wr_kwh   = (ac_clipped  * dt_h).sum() / 1000
    
    # ------------------------------------------------------------
    #   Verluste (Temp / Low-Irradiance) sauber getrennt
    # ------------------------------------------------------------
    # Temperatur-Verlust = Differenz real ↔ 25 °C
    temp_loss_pct = (
        (1 - total_dc_kwh / total_dc_25_kwh) * 100 if total_dc_25_kwh else 0.0
    )

    # Low-Irradiance-Verlust = fester Low-Irradiance-Verlust [%]
    LOWIRR_THRESH = 200          # W/m²
    poa_eff_all   = pd.concat(poa_eff_list, axis=1).mean(axis=1)

    frac_lowirr       = (poa_eff_all < LOWIRR_THRESH).mean()          # 0 … 1
    lowirr_loss_pct   = round(frac_lowirr * 3.0, 2)                   # max ≈ 3 %

    avg_inv_eff_pct = (total_ac_wr_kwh / total_dc_kwh * 100) if total_dc_kwh else 0.0
    avg_inv_eff_pct = min(avg_inv_eff_pct, 100.0)
    
    if total_dc_ref_kwh:                        # Division-durch-0 abfangen
        temp_low_loss_pct = (1 - total_dc_kwh / total_dc_ref_kwh) * 100
    else:
        temp_low_loss_pct = 0.0
        
    # Energie-Summary
    dbg
        
    # (optional) mittlere Zell-Temp als Plausibilität
    # (liegt oft bei 35–45 °C für Aufdach-Module in DE)
    mean_t_cell = (
        pvlib.temperature.faiman(
            poa_global = poa,
            temp_air   = df_weather["temp_air"],
            wind_speed = df_weather.get("wind_speed", 1.0),
            u0 = 20, u1 = 0.0
        ).mean()
    )
    # benutze _compute_losses aus vorheriger Anleitung
    # system_loss = Optik + Temp + Inverter
    opt_loss_pct  = 100 * (1 - poa_eff.sum() / poa.sum())
    inv_loss_pct  = 100 * (1 - total_ac_wr_kwh / total_dc_kwh)
    # opt_wr_loss_pct = Optik + WR
    opt_wr_loss_pct = opt_loss_pct + inv_loss_pct

    system_loss_pct = (
        opt_loss_pct +
        temp_loss_pct +
        lowirr_loss_pct +
        inv_loss_pct
    )
    total_loss_pct  = system_loss_pct + sum(settings.losses_pct.values())

    # -------------------------------------------------------------------
    
    derate = 1.0 - sum(settings.losses_pct.values()) / 100.0
    ac_net = ac_clipped * derate * eta_sys

    total_ac_net_kwh   = (ac_net * dt_h).sum() / 1000        # Wh
    #logger.debug(f"Ø System-Wirkungsgrad (AC_net/DC)  : {avg_sys_eff_pct:5.2f} %")

    prod_kw = ac_net / 1000.0              #  kW (Momentanleistung)

    # ------------------------------------------------------------
    #   EINHEITLICH auf kWh pro Zeitschritt umstellen
    # ------------------------------------------------------------
    #dt_h = settings.timestep_min / 60.0      # z. B. 15 min → 0.25 h

    # ----- (1) gewünschte Jahre -------------------------------------------------
    mask_prod = (prod_kw.index.year >= start_year) & (prod_kw.index.year <= end_year)
    prod_kw   = prod_kw.loc[mask_prod]
    direct_frac = direct_frac.loc[prod_kw.index]     # Align!

    # ----- (2) optionale monatliche Verschattung -------------------------------
    if settings.shading_mode == "monatlich":
        shade_arr = prod_kw.index.month.map(
            lambda m: settings.shading_monthly_pct.get(m, 0) / 100.0
        )
        # nur der Direktanteil wird verschattet
        prod_kw *= 1 - direct_frac * shade_arr

    # ----- (3) *jetzt erst* kWh/Schritt berechnen ------------------------------
    energy_ts = prod_kw * dt_h                #  ← überschreibt den alten Wert

    # ------------------------------------------------------------------
    # 5) Jahres‑/Monats‑Erträge
    # ------------------------------------------------------------------
    _report(50)
    yearly_kwh   = energy_ts.groupby(energy_ts.index.year).sum()
    year_prod_kwh = yearly_kwh.mean()

    monthly = (
        energy_ts.groupby([energy_ts.index.year, energy_ts.index.month]).sum()
        .unstack(0)
        .mean(axis=1)
    )

    # ------------------------------------------------------------------
    # 6) Verbrauchsprofil
    # ------------------------------------------------------------------
    ### NEW BEGIN ### -------- 6) Verbrauchsprofil  (Fein‑Raster) ------------
    idx_m  = prod_kw.index.month - 1
    idx_h  = prod_kw.index.hour
    idx_q  = (prod_kw.index.minute // 15)   # Viertelstunden‑Gewicht

    m_w = _monthly_w[idx_m]                                 # 12‑Array
    h_w = (_daily_ret if settings.profile == "retiree" else _daily_work)[idx_h]

    # innerhalb einer Stunde gleich verteilen
    q_w = np.ones_like(idx_q) / (60 / settings.timestep_min)

    weights = m_w * h_w * q_w
    weights /= weights.sum()
    consumption = settings.annual_load_kwh * n_years * weights   # ❶

    # ------------------------------------------------------------
    # Helper: Monatsmittel aus Array oder Series (kWh / Monat)
    # ------------------------------------------------------------
    def _mean_monthly_any(arr) -> pd.Series:
        if isinstance(arr, pd.Series):
            s = arr
        else:
            s = pd.Series(arr, index=energy_ts.index)
        return (s.groupby([s.index.year, s.index.month])
                .sum().unstack(0).mean(axis=1))

    # ------------------------------------------------------------------
    # 7) Batterie-Simulation (feintaktig, kWh-basiert)
    # ------------------------------------------------------------------
    _report(70)

    # ────────────── Hilfs-Konstanten ──────────────────────────────────
    #dt_h = settings.timestep_min / 60.0          # z B 15 min → 0.25 h

    # ────────────── Akku-Parameter einlesen ───────────────────────────
    batt_cap = 0.0                               # kWh nominelle Kapazität
    if batt_obj and settings.batt_units > 0 and sys_obj.get("storage_supported"):
        batt_cap    = batt_obj["capacity_wh"] * settings.batt_units / 1000.0
        standby_kw  = batt_obj.get("standby_power_w", 0) * settings.batt_units / 1000.0
        eta_rt      = batt_obj.get("roundtrip_efficiency_percent", 100) / 100.0
        eta_ch = eta_dis = math.sqrt(eta_rt)     # Lade/Entlade-Wirkungsgrad
        p_ch_max_kw  = settings.batt_units * batt_obj.get("max_charge_power_w",    800) / 1000.0
        p_dis_max_kw = settings.batt_units * batt_obj.get("max_discharge_power_w", 1200) / 1000.0
        soc_min = settings.soc_min_pct / 100.0
        soc_max = settings.soc_max_pct / 100.0
    else:
        standby_kw = p_ch_max_kw = p_dis_max_kw = 0.0
        eta_ch = eta_dis = eta_rt = 1.0
        soc_min = soc_max = 0.0

    # kW  →  kWh pro Zeitschritt
    standby_ts   = standby_kw   * dt_h
    p_ch_max_ts  = p_ch_max_kw  * dt_h
    p_dis_max_ts = p_dis_max_kw * dt_h                    # kWh / Schritt
    n_steps      = len(energy_ts)

    # ----------  Hilfs-Funktion: Akku-Simulation pro Schritt  ----------
    def _simulate(disabled: set[int]) -> tuple[np.ndarray, np.ndarray,
                                            np.ndarray, np.ndarray]:
        """liefert  direct_use, batt_out, idle_loss, charge_in  (je kWh / Schritt)"""
        direct_use = np.zeros(n_steps)
        batt_out   = np.zeros(n_steps)
        idle_loss  = np.zeros(n_steps)
        charge_in  = np.zeros(n_steps)
        state      = 0.0                                    # SoC [kWh]

        for i, (ts, pv_kwh, load_kwh) in enumerate(
                zip(energy_ts.index, energy_ts.values, consumption)):

            direct             = min(pv_kwh, load_kwh)
            direct_use[i]      = direct
            surplus            = pv_kwh - direct
            deficit            = load_kwh - direct
            month_off          = ts.month in disabled         # Akku abgeklemmt?

            if batt_cap and not month_off:
                # Stand-by
                if state > batt_cap * soc_min:
                    idle = min(state - batt_cap * soc_min, standby_ts)
                    state      -= idle
                    idle_loss[i] = idle
                # Laden
                if surplus > 0 and state < batt_cap * soc_max:
                    room = batt_cap * soc_max - state
                    ch   = min(surplus, p_ch_max_ts, room)
                    charge_in[i] = ch
                    state       += ch * eta_ch
                    surplus     -= ch
                # Entladen
                if deficit > 0:
                    avail = state - batt_cap * soc_min
                    di    = min(deficit, p_dis_max_ts, avail)
                    batt_out[i] = di * eta_dis
                    state       -= di

        return direct_use, batt_out, idle_loss, charge_in
    
    def _mean_monthly(arr: np.ndarray) -> pd.Series:
        s = pd.Series(arr, index=prod_kw.index)
        return (
            s.groupby([s.index.year, s.index.month]).sum()
            .unstack(0)
            .mean(axis=1)
        )
    
    # ------------------------------------------------------------------
    # 7a) erste Simulation  →  gute / schlechte Monate finden
    # ------------------------------------------------------------------
    # Speicher-Daten
    dbg("BATTS", "Speicher: Kapazität={} kWh  Einheiten={}  "
                "Lade-P_max={} kW  Entlade-P_max={} kW  "
                "Round-Trip={}  SoC-Grenzen={}…{} %",
        fmt1(batt_cap), settings.batt_units,
        fmt1(p_ch_max_kw), fmt1(p_dis_max_kw),
        pct1(eta_rt*100), settings.soc_min_pct, settings.soc_max_pct,
    )

    direct_use, batt_out, idle_loss, charge_in = _simulate(set())

    disabled_months: list[int] = []
    if settings.optimize_storage and batt_cap:
        def _mon(arr):                                     # Monats-Mittel
            return (pd.Series(arr, index=energy_ts.index)
                    .groupby([energy_ts.index.year, energy_ts.index.month])
                    .sum().unstack(0).mean(axis=1))

        mon_use  = _mean_monthly_any(batt_out)
        mon_idle = _mean_monthly_any(idle_loss)
        mon_eta  = _mean_monthly_any(charge_in * (1 - eta_rt))

        for m in range(1, 13):
            if mon_use[m] - mon_idle[m] - mon_eta[m] <= 0:
                disabled_months.append(m)

        # 7b) zweite Simulation  – Akku in „roten“ Monaten abgeklemmt
        if disabled_months:
            dset = set(disabled_months)
            direct_use, batt_out, idle_loss, charge_in = _simulate(dset)

    # ------------------------------------------------------------------
    # 7c) Jahres-Kennzahlen  (inkl. zusätzl. Wechselrichter-Verluste Batterie)
    # ------------------------------------------------------------------
    n_years = len(yearly_kwh)

    # ------------------------------------------------------------
    #   Wirkungsgrad der Batterie-Entladung je Zeitschritt
    # ------------------------------------------------------------
    # 1) Instantane DC-Leistung der Entladung (W)
    batt_p_dc_w = pd.Series(batt_out / dt_h * 1000, index=energy_ts.index)

    # 2) η(P) anhand der gleichen WR-Kennlinie bestimmen
    batt_eta = _interp_eta(
        batt_p_dc_w,
        curve_w,      # kommt noch aus Abschnitt 4b
        curve_pct,
        eta_fallback=(sys_obj.get("ac_efficiency_percent") or
                    inv_obj.get("ac_efficiency_percent", 100)) / 100,
    )

    # 3) AC-Energie nach WR
    batt_out_ac = batt_out * batt_eta

    # ------------------------------------------------------------
    #  WR-Statistik um Batterie-Anteil erweitern
    # ------------------------------------------------------------
    batt_dc_kwh = batt_out.sum()        # DC vor WR
    batt_ac_kwh = batt_out_ac.sum()        # AC nach WR

    # Systemwirkungsgrad (PV + Batterie) – korrekt gewichtet
    if settings.batt_units and batt_ac_kwh > 0:
        # gesamte DC-Eingangsenergie = PV-DC + Batterie-DC
        sys_dc = total_dc_kwh + batt_dc_kwh
        sys_ac = total_ac_net_kwh + batt_ac_kwh
        avg_sys_eff_pct = 100.0 * sys_ac / sys_dc
    elif total_dc_kwh:
        # kein Speicher oder kein Batterie-Output → reiner PV-Fall
        avg_sys_eff_pct = 100.0 * total_ac_net_kwh / total_dc_kwh
    else:
        avg_sys_eff_pct = 0.0

    # Debug-Ausgabe anpassen
    dbg("SYSTM", "Systemwirkungsgrad={}  (PV_AC={} kWh  Bat_AC={} kWh  DC_gesamt={} kWh)",
        pct1(avg_sys_eff_pct),
        fmt1(total_ac_net_kwh),
        fmt1(batt_ac_kwh),
        fmt1(sys_dc if settings.batt_units else total_dc_kwh),
    )

    #  PV + Batterie zusammenfassen
    total_dc_wr_kwh_comb = total_dc_kwh    + batt_dc_kwh
    total_ac_wr_kwh_comb = total_ac_wr_kwh + batt_ac_kwh

    # Neuer gewichteter WR-Wirkungsgrad
    avg_inv_eff_pct = 0.0
    if total_dc_wr_kwh_comb:
        avg_inv_eff_pct = total_ac_wr_kwh_comb / total_dc_wr_kwh_comb * 100
        avg_inv_eff_pct = min(avg_inv_eff_pct, 100.0)
        
        # Gesamt-Wirkungsgrad (WR+Bat)
        dbg("GESAM", "DC_gesamt={} kWh  AC_gesamt={} kWh  Gesamt-Wirkungsgrad={}",
            fmt1(total_dc_wr_kwh_comb),
            fmt1(total_ac_wr_kwh_comb),
            pct1(avg_inv_eff_pct),
        )

    # Verluste neu berechnen
    inv_loss_pct = 100.0 - avg_inv_eff_pct

    if sys_type == "charger_only":
        charger_loss_pct = 100 - sys_obj.get("dc_dc_efficiency_percent", 100)
        #logger.debug(f"DC-DC-Charger-Verlust       : {charger_loss_pct:.2f} %")


    direct_use_kwh = direct_use.sum()   / n_years
    batt_use_kwh   = batt_out_ac.sum()  / n_years
    direct_use_kwh = direct_use.sum() / n_years

    if sys_type == "charger_only":
        batt_use_kwh   = batt_out_ac.sum() / n_years if batt_out_ac is not None else 0
    else:
        batt_use_kwh   = batt_out.sum()   / n_years

    total_use_kwh  = direct_use_kwh + batt_use_kwh
    
    # Jahres-Ergebnisse
    dbg("RESUL", "Jahresertrag={} kWh  Eigenverbrauch_DC={} kWh  "
                "Batterie_AC={} kWh  Selbstnutzungsquote={}",
        fmt1(year_prod_kwh),
        fmt1(direct_use_kwh),
        fmt1(batt_use_kwh),
        pct1(total_use_kwh / year_prod_kwh * 100),
    )

    # ------------------------------------------------------------------
    # 8) Monats-Aggregationen (für Diagramme / Überschuss)
    # ------------------------------------------------------------------
    _report(80)

    mon_prod      = _mean_monthly_any(energy_ts)                # PV-Ertrag
    mon_use_no_st = _mean_monthly_any(direct_use)               # ohne Akku
    mon_sur_no_st = (mon_prod - mon_use_no_st).clip(lower=0)

    if batt_cap:
        mon_use_st = _mean_monthly_any(direct_use + batt_out)   # mit Akku
        mon_sur_st = (mon_prod - mon_use_st).clip(lower=0)
    else:
        mon_use_st = mon_sur_st = None

    # Jahres-Überschuss (kWh)
    year_sur_no  = mon_sur_no_st.sum()
    year_sur_yes = mon_sur_st.sum() if mon_sur_st is not None else year_sur_no
    
    # ------------------------------------------------------------------
    # 9) Wirtschaftlichkeit & Umweltwirkung (Fortsetzung)
    # ------------------------------------------------------------------
    _report(90)
    save_wo = _escalated_cashflow(direct_use_kwh, settings.price_eur_per_kwh, settings.price_escalation_pct, settings.operating_years)
    save_w = _escalated_cashflow(total_use_kwh, settings.price_eur_per_kwh, settings.price_escalation_pct, settings.operating_years)

    n_modules = sum(mp.n_modules for mp in settings.mppts)
    cost_wo = n_modules * settings.cost_module_eur + settings.cost_inverter_eur + settings.cost_install_eur - settings.subsidy_eur
    cost_w = cost_wo + settings.cost_battery_eur * settings.batt_units

    bal_wo = save_wo - cost_wo
    bal_w = save_w - cost_w

    stg_wo = cost_wo / (direct_use_kwh * settings.operating_years) * 100 if direct_use_kwh else float("inf")
    stg_w = cost_w / (total_use_kwh * settings.operating_years) * 100 if total_use_kwh else float("inf")

    pay_wo = cost_wo / (direct_use_kwh * settings.price_eur_per_kwh) if direct_use_kwh else float("inf")
    pay_w = cost_w / (total_use_kwh * settings.price_eur_per_kwh) if total_use_kwh else float("inf")

    co2_wo = direct_use_kwh * settings.operating_years * settings.co2_factor
    co2_w = total_use_kwh * settings.operating_years * settings.co2_factor
    
    # Spaßige CO₂-Visualisierung:    0,173 kg CO₂ pro km Pkw (UBA-Durchschnitt)
    km_eq_wo = co2_wo / 0.173
    km_eq_w  = co2_w  / 0.173

    eigen_wo = direct_use_kwh / year_prod_kwh * 100 if year_prod_kwh else 0
    eigen_w = total_use_kwh / year_prod_kwh * 100 if year_prod_kwh else 0

    # ------------------------------------------------------------------
    # --- Verluste korrekt zusammentragen ------------------------------
    # ------------------------------------------------------------------
    # ❶ Einzelwerte aus den Settings
    loss_leitung       = settings.losses_pct.get("Leitungsverluste",    0.0)
    loss_verschmutzung = settings.losses_pct.get("Verschmutzung",       0.0)
    loss_mismatch      = settings.losses_pct.get("Modul-Mismatch",      0.0)
    loss_lid           = settings.losses_pct.get("LID",                 0.0)
    loss_toleranz      = settings.losses_pct.get("Nameplate-Toleranz",  0.0)
    loss_alterung      = settings.losses_pct.get("Alterung",            0.0)

    # alles, was nicht explizit aufgeführt ist
    loss_sonstige = sum(
        v for k, v in settings.losses_pct.items()
        if k not in {
            "Leitungsverluste", "Verschmutzung", "Modul-Mismatch",
            "LID", "Nameplate-Toleranz", "Alterung"
        }
    )

    # ------------------------------------------------------------------
    #   Verluste relativ zum DC-Eingang
    # ------------------------------------------------------------------
    # Wechselrichter-Verlust (PV+Bat-DC → AC) [%]
    inv_loss_pct = (
        100.0 * (1.0 - total_ac_wr_kwh_comb / total_dc_wr_kwh_comb)
        if total_dc_wr_kwh_comb else 0.0
    )

    # Verschattungs-Verlust [%] – wurde im MPPT-Loop gesammelt
    shading_loss_pct = 0.0
    if dc_noshade_total.sum() > 0:
        dc_noshade_kwh = (dc_noshade_total * dt_h).sum() / 1000
        shading_loss_pct = round(
            (1 - total_dc_kwh / dc_noshade_kwh) * 100, 2
        )
    shading_loss_pct = max(0.0, shading_loss_pct)

    # Low-Irr-Verlust [%] – bereits dynamisch aus POA_eff abgeleitet
    # Variable lowirr_loss_pct ist vorher definiert

    # Benutzerdefinierte System-Verluste [%]
    sys_loss_pct = sum(settings.losses_pct.values())

    # Gesamtverlust = WR + Verschattung + Low-Irr + System
    total_loss_pct = round(
        inv_loss_pct + shading_loss_pct + lowirr_loss_pct + sys_loss_pct, 2
    )

    # ------------------------------------------------------------------
    #   Brutto-/Netto-AC-Ertrag  (bleibt unverändert)
    # ------------------------------------------------------------------
    ertrag_brutto_kwh = total_dc_wr_kwh_comb / n_years
    ertrag_netto_kwh  = total_ac_net_kwh / n_years
    ertrag_brutto_pct = 100.0
    ertrag_netto_pct  = (
        100.0 * ertrag_netto_kwh / ertrag_brutto_kwh
        if ertrag_brutto_kwh else 0.0
    )
    
    # ------------------------------------------------------------------
    # Speicherkapazität
    # ------------------------------------------------------------------
    # Nominale Kapazität in kWh (batt_cap steht schon weiter oben in kWh)
    soc_min = settings.soc_min_pct / 100.0
    soc_max = settings.soc_max_pct / 100.0
    brutto_kwh = batt_cap
    netto_kwh  = brutto_kwh * (soc_max - soc_min)
    
    # Rundum-Effizienz aus Spezifikation (z.B. 90 %)
    rt_pct = batt_obj.get("roundtrip_efficiency_percent", 100) if batt_obj else 100
    rt = rt_pct / 100.0

    # effektive nutzbare Kapazität je Zyklus
    effective_netto_kwh = netto_kwh * rt

    # ------------------------------------------------------------------
    # 10) Ergebnis-Tabellen
    # ------------------------------------------------------------------
    rows_gain = [
        ("Stromerzeugung pro Jahr",        f"{year_prod_kwh:.0f} kWh",      f"{year_prod_kwh:.0f} kWh"),
        ("Überschuss (Einspeisung)",       f"{year_sur_no:.0f} kWh",        f"{year_sur_yes:.0f} kWh"),
        ("Vermiedener Strombezug pro Jahr",f"{direct_use_kwh:.0f} kWh",     f"{total_use_kwh:.0f} kWh"),
        ("Eigenverbrauchsanteil",          f"{eigen_wo:.0f} %",             f"{eigen_w:.0f} %"),
        ("Autarkiegrad",                   f"{direct_use_kwh/settings.annual_load_kwh*100:.0f} %", f"{total_use_kwh/settings.annual_load_kwh*100:.0f} %"),
    ]

    rows_econ = [
        ("Jährl. Ersparnis",               f"{direct_use_kwh*settings.price_eur_per_kwh:.0f} €",    f"{total_use_kwh*settings.price_eur_per_kwh:.0f} €"),
        ("Ersparnis gesamt",               f"{save_wo:.0f} €",      f"{save_w:.0f} €"),
        ("Anschaffungskosten",             f"{cost_wo:.0f} €",      f"{cost_w:.0f} €"),
        ("Bilanz gesamt",                  f"{bal_wo:.0f} €",       f"{bal_w:.0f} €"),
        ("Stromgestehungskosten (ct/kWh)", f"{stg_wo:.1f} ct",      f"{stg_w:.1f} ct"),
        ("Amortisationszeit",              f"{pay_wo:.1f} J",       f"{pay_w:.1f} J"),
    ]

    rows_env = [
        ("CO₂-Einsparung",                 f"{co2_wo:.0f} kg",      f"{co2_w:.0f} kg"),
        ("PKW-Fahrstrecke äquiv.",         f"{km_eq_wo:,.0f} km",   f"{km_eq_w:,.0f} km"),
    ]

    # ------------------------------------------------------------------
    # 10b) Verluste-Breakdown
    # ------------------------------------------------------------------
    rows_loss = [
        ("Wechselrichter-Verlust",  f"{inv_loss_pct:.1f} %",       f"{inv_loss_pct:.1f} %"),
        ("Low-Irradiance-Verlust",  f"{lowirr_loss_pct:.1f} %",    f"{lowirr_loss_pct:.1f} %"),
        ("System-Verluste (User)",  f"{sys_loss_pct:.1f} %",       f"{sys_loss_pct:.1f} %"),
        ("Verschattung",            f"{shading_loss_pct:.1f} %",   f"{shading_loss_pct:.1f} %"),
        ("Gesamtverlust",           f"{total_loss_pct:.1f} %",     f"{total_loss_pct:.1f} %"),
    ]

    # ------------------------------------------------------------------
    # 10c) Wirkungsgrade
    # ------------------------------------------------------------------
    rows_efficiency = [
        ("Systemwirkungsgrad",      f"{avg_sys_eff_pct:.1f} %",     f"{avg_sys_eff_pct:.1f} %"),    # reiner WR-Pfad (plus Bat-WR)
        ("Gesamt-Wirkungsgrad",     f"{avg_inv_eff_pct:.1f} %",     f"{avg_inv_eff_pct:.1f} %"),    # Wechselrichter-Verluste plus Kabel, Verschmutzung, LID, Toleranzen …
        # ("Ø Zelltemperatur",        f"{mean_t_cell:.1f} °C",        f"{mean_t_cell:.1f} °C"),
    ]
    
    # ------------------------------------------------------------------
    # 10d) Speicherkapazität
    # ------------------------------------------------------------------
    # gesamte Kapazität ohne Grenzen durch SoC
    display_brutto = f"{brutto_kwh:.3f} kWh" if brutto_kwh > 0 else "---"
    # begrenzt durch Lade-/Entlade-SoC
    display_netto  = f"{netto_kwh:.3f} kWh"  if brutto_kwh > 0 else "---"
    # Nutzkapazität × Round-Trip-Effizienz
    display_eff_netto = f"{effective_netto_kwh:.3f} kWh" if brutto_kwh > 0 else "---"

    rows_bat = [
        ("Nennkapazität",  display_brutto, display_brutto),
        ("Nutzkapazität",   display_netto, display_netto),
        ("Effektive Nutzkapazität",  display_eff_netto, display_eff_netto),
    ]

    # ------------------------------------------------------------------
    #   Debug-Ausgaben
    # ------------------------------------------------------------------
    # Verluste
    dbg("LOSS ", "WR={}  Verschattung={}  Low-Irr={}  System={}  Gesamt={}",
        pct1(inv_loss_pct), pct1(shading_loss_pct),
        pct1(lowirr_loss_pct), pct1(sys_loss_pct),
        pct1(total_loss_pct),
    )

    # Prüfsumme
    dbg("CHECK", "Brutto={} kWh → Netto={} kWh  (Netto={})  |  Gesamtverlust={}",
        fmt1(ertrag_brutto_kwh),
        fmt1(ertrag_netto_kwh),
        pct1(ertrag_netto_pct),
        pct1(total_loss_pct),
    )

    # PVGIS-Cache
    dbg("CACHE", "PVGIS-Zwischenspeicher: {} Einträge", len(_PVGIS_CACHE))
    
    loss_modul_pct   = 0.0                   # wir fassen alle Modulverluste im WR zusammen
    loss_wr_pct      = inv_loss_pct          # Wechselrichter-Verlust
    loss_system_pct  = sys_loss_pct          # System-Verluste aus den Settings
    loss_total_pct   = total_loss_pct        # Summe aus beidem

    # ------------------------------------------------------------------
    # 11.2) Rückgabe
    # ------------------------------------------------------------------
    _report(100)
    return {
        # Monatliche und jährliche Erträge für Graphen etc.
        "mon_prod":         mon_prod,
        "mon_use_no_st":    mon_use_no_st,
        "mon_sur_no_st":    mon_sur_no_st,
        "mon_use_st":       mon_use_st,
        "mon_sur_st":       mon_sur_st,
        "rows_gain":        rows_gain,
        "rows_econ":        rows_econ,
        "rows_env":         rows_env,
        "rows_gain":        rows_gain,
        "rows_econ":        rows_econ,
        "rows_env":         rows_env,
        "rows_loss":        rows_loss,
        "rows_efficiency":  rows_efficiency,

        # --- Einzelverluste (Settings) ---
        "leitungsverlust_pct": round(loss_leitung,       2),
        "verschmutzung_pct":   round(loss_verschmutzung, 2),
        "mismatch_pct":        round(loss_mismatch,      2),
        "lid_pct":             round(loss_lid,           2),
        "toleranz_pct":        round(loss_toleranz,      2),
        "alterung_pct":        round(loss_alterung,      2),
        "sonstige_pct":        round(loss_sonstige,      2),
        "lowirr_pct":          round(lowirr_loss_pct,    2),

        # --- Brutto/Netto-Ertrag ---
        "ertrag_brutto_pct": ertrag_brutto_pct,
        "ertrag_netto_pct":  round(ertrag_netto_pct, 2),
        "ertrag_brutto_kwh": round(ertrag_brutto_kwh, 2),
        "ertrag_netto_kwh":  round(ertrag_netto_kwh, 2),

        # --- Gesamtverluste nach Gruppe ---
        "loss_modul_pct": 0.0,
        "loss_wr_pct":   round(inv_loss_pct, 2),
        "loss_system_pct": round(sys_loss_pct, 2),
        "loss_total_pct":  round(total_loss_pct, 2),

        # --- DC-Energie, POA ---
        "dc_stc_kwh":       round(total_dc_ref_kwh / n_years, 2),
        "dc_real_kwh":      round(total_dc_kwh      / n_years, 2),
        "ac_raw_kwh":       round(total_ac_wr_kwh   / n_years, 2),
        "poa_global_mean":  round(poa.mean(), 2),
        "poa_eff_mean":     round(poa_eff.mean(), 2),

        # --- interne Wirkungsgrade / Verluste ---
        "opt_loss_pct":     round(opt_loss_pct,   2),
        "inv_loss_pct":     round(inv_loss_pct,   2),
        "temp_loss_pct":    round(temp_loss_pct,  2),
        
        # --- Speicherkapazität
        "rows_bat":         rows_bat,

        # --- Sonstige Info ---
        "has_store":              bool(batt_cap),
        "year_surplus_no_st":     mon_sur_no_st.sum(),
        "year_surplus_st":        mon_sur_st.sum() if mon_sur_st is not None else 0,
        "disabled_months":        disabled_months,
        "avg_inverter_eff_percent": avg_inv_eff_pct,
        "avg_system_eff_percent":   avg_sys_eff_pct,
        "loss_total_percent":       round(loss_total_pct, 2),
        "internal_losses_pct": {
            "Temp":    round(temp_loss_pct,  2),
            "LowIrr":  round(lowirr_loss_pct, 2),
            "OptWR":   round(opt_wr_loss_pct, 2),
        },
        "user_system_loss_pct": round(sum(settings.losses_pct.values()), 2),
    }
    
# ------------------------------------------------------------------
#  Hilfs-Routine: gewichteter Gesamt-Wirkungsgrad
# ------------------------------------------------------------------
def calculate_avg_system_efficiency(
    generator_configs: List[GeneratorConfig],
    sys_obj: dict,
    inverter_obj: dict | None = None,
) -> float:
    # """
    # Liefert **einen** repräsentativen Mittelwert des Gesamt-DC→AC-
    # Wirkungsgrads – abgeleitet aus der Kennlinie des integrierten
    # bzw. externen Wechselrichters.

    # Strategie
    # ---------
    # 1.  Wir benutzen die Effizienz-Kennlinie (W-/-%-Punkte) und
    #     berechnen die Fläche **unter** der Kurve (Integral).
    #     Die Normierung auf die Nennleistung ergibt – ähnlich
    #     einer Jahres-Kennzahl – einen gewichteten Mittelwert, der
    #     realistisch unterhalb des Maximalpunkts liegt.

    # 2.  Ist keine Kurve vorhanden, wird auf
    #     ``*_ac_efficiency_percent`` zurückgegriffen.

    # 3.  Als „Leistungs-Verteilung“ dient die reine **Gleichverteilung**
    #     zwischen 0 W und P\ :sub:`max`.  Ohne tatsächliche
    #     Produktions-Simulation ist das der beste „pragmatische“
    #     Schätzer – typischerweise ergibt er 92–95 % statt 97 %.

    # Parameters
    # ----------
    # generator_configs
    #     Liste der GeneratorConfig-Objekte (nur für die Gesamt-DC-Leistung
    #     benötigt).
    # sys_obj, inverter_obj
    #     Die passenden Dicts aus *pv_systems.json* bzw. *inverters.json*.

    # Returns
    # -------
    # float
    #     Mittlerer Wirkungsgrad in **Prozent** (0 … 100).
    # """
    # ------------------------------------------------------------
    #  Gesamt-DC-Leistung (Wp)
    # ------------------------------------------------------------
    p_dc_nom = sum(g.n_modules * g.wp_module for g in generator_configs)
    if p_dc_nom <= 0:
        return 0.0

    # ------------------------------------------------------------
    #  Kurve auswählen
    # ------------------------------------------------------------
    if sys_obj.get("inverter_integrated"):
        curve_w   = sys_obj.get("efficiency_curve_w", [])
        curve_pct = sys_obj.get("efficiency_curve_pct", [])
        eta_fixed = sys_obj.get("ac_efficiency_percent")
    else:
        curve_w   = (inverter_obj or {}).get("efficiency_curve_w", [])
        curve_pct = (inverter_obj or {}).get("efficiency_curve_pct", [])
        eta_fixed = (inverter_obj or {}).get("ac_efficiency_percent")

    # ------------------------------------------------------------
    # 1.  Fall: komplette Kennlinie vorhanden  →  Flächenmittel
    # ------------------------------------------------------------
    if curve_w and curve_pct and len(curve_w) == len(curve_pct):
        import numpy as np

        w   = np.array(curve_w,   dtype=float)
        pct = np.array(curve_pct, dtype=float)

        # -- sicherstellen, dass Nennleistung am Ende steht
        if w[-1] < p_dc_nom:
            w   = np.append(w,   p_dc_nom)
            pct = np.append(pct, pct[-1])

        # Trapez-Integration  (∫ η(P) dP)
        area = np.trapz(pct / 100.0, w)

        # Normieren auf P_max  ⇒  gewichteter Mittelwert
        eta_mean = area / w[-1] * 100.0
        return float(round(eta_mean, 1))

    # ------------------------------------------------------------
    # 2.  Fall: fester Wirkungsgrad vorhanden
    # ------------------------------------------------------------
    if eta_fixed is not None:
        return float(eta_fixed)

    # ------------------------------------------------------------
    # 3.  Fallback – ideal
    # ------------------------------------------------------------
    return 100.0

# ------------------------------------------------------------------
#   Verlust-Aufschlüsselung (korrekte Vorzeichen)
# ------------------------------------------------------------------
def _compute_losses(temp_low_loss_pct: float,
                    avg_inv_eff_pct: float,
                    user_losses_pct: dict[str, float]
                    ) -> tuple[float, float, float]:
    # """
    # Liefert  (opt_wr_loss_pct, system_loss_pct, total_loss_pct)

    # • opt_wr_loss_pct – Verluste durch Optik-/IAM + Wechselrichter
    # • system_loss_pct – opt_wr_loss_pct + Temp/Low-Irradiance-Verlust
    # • total_loss_pct  – system_loss_pct + alle benutzer­definierten Verluste
    # """
    opt_wr_loss_pct = 100.0 - avg_inv_eff_pct          # richtiges Vorzeichen
    system_loss_pct = opt_wr_loss_pct + temp_low_loss_pct
    total_loss_pct  = system_loss_pct + sum(user_losses_pct.values())
    return opt_wr_loss_pct, system_loss_pct, total_loss_pct

def calculate_avg_storage_efficiency(battery_spec: dict) -> float:
    # """
    # Liefert den mittleren Wirkungsgrad des Speichers (Roundtrip oder Lade/Entlade-Effizienz).

    # Args:
    #     battery_spec: Dict des Speichermodells aus batteries.json
    # Returns:
    #     Durchschnittlicher Wirkungsgrad in Prozent.
    # """
    rt = battery_spec.get("roundtrip_efficiency_percent")
    if rt is not None:
        return float(rt)
    cd = battery_spec.get("charge_discharge_efficiency_percent")
    if cd is not None:
        return float(cd)
    return 100.0


def compute_total_losses(settings: Settings) -> float:
    # """
    # Summiert alle konfigurierten System-Verluste aus den Simulationseinstellungen.

    # Args:
    #     settings: Settings-Objekt mit .losses_pct Dict[str, float]
    # Returns:
    #     Summe aller Verlustprozente.
    # """
    return float(sum(settings.losses_pct.values()))

def get_battery_spec(model: str) -> dict:
    # """
    # Liefert die Spezifikationen für ein gegebenes Batteriemodell.

    # Args:
    #     model: Modellname aus batteries.json
    # Returns:
    #     Dictionary mit den Spezifikationen.
    # Raises:
    #     KeyError: Wenn das Modell nicht in den geladenen Daten gefunden wird.
    # """
    spec = _batt_by_model.get(model)
    if spec is None:
        raise KeyError(f"Batteriemodell '{model}' nicht gefunden.")
    return spec