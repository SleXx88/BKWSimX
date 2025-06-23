# src/launch_debug.py
"""
Startet BKWSimX und fängt alle ungefangenen Exceptions ab,
damit man sie in der Konsole sieht UND noch kopieren kann.
"""

import traceback, sys

def main():
    try:
        # ----- dein eigentliches Programm ------ #
        from main import main as real_main
        real_main()
    except Exception:
        # komplette Rückverfolgung auf Bildschirm
        traceback.print_exc()
        # zusätzlich in Datei schreiben
        with open("error.log", "w", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        print("\nEin Fehler ist aufgetreten! "
              "Die Details wurden in error.log gespeichert.")
    finally:
        input("\n[ENTER] drücken, um das Fenster zu schließen …")

if __name__ == "__main__":
    main()
