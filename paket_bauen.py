# Pegellotse — Schallpegel-Monitoring fuer Veranstaltungen
# Copyright (C) 2026 Niels
#
# Dieses Programm ist freie Software: Sie koennen es weitergeben und/oder
# veraendern unter den Bedingungen der GNU General Public License, Version 3,
# wie von der Free Software Foundation veroeffentlicht.
#
# Die Veroeffentlichung erfolgt in der Hoffnung, dass es nuetzlich ist, aber
# OHNE JEDE GEWAEHRLEISTUNG — sogar ohne die implizite Gewaehrleistung der
# MARKTFAEHIGKEIT oder EIGNUNG FUER EINEN BESTIMMTEN ZWECK. Einzelheiten in
# der GNU General Public License, mitgeliefert als Datei LICENSE.

"""
paket_bauen.py — baut die portable Ordnerversion.

Das Paket bringt ein eigenes Python von python.org mit (signiert von der
Python Software Foundation) und braucht daher weder eine Installation noch eine
selbst gebaute EXE. Damit laeuft es auch dort, wo unsignierte Programme
blockiert werden.

    python paket_bauen.py                 baut nach  paket/Pegellotse
    python paket_bauen.py --zip           packt zusaetzlich ein ZIP
    python paket_bauen.py --python 3.12.10   andere Python-Version einbetten

Das Skript laeuft selbst unter dem normalen Python und ruft nur Python und pip
auf — es entsteht an keiner Stelle eine neue ausfuehrbare Datei.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HIER = Path(__file__).resolve().parent
STANDARD_PYTHON = "3.12.10"

# Was ins Paket gehoert
DATEIEN = ["pegellotse.py", "messkern.py", "selftest.py", "requirements.txt",
           "README.md", "LICENSE"]
ORDNER = ["beispiel"]   # templates wird gesondert behandelt, siehe baue()

STARTER = """@echo off
title Pegellotse
cd /d "%~dp0"

if not exist "python\\python.exe" goto :kein_python

echo Pegellotse startet ...
echo Dieses Fenster offen lassen - Schliessen beendet die Messung.
echo.
"python\\python.exe" pegellotse.py
echo.
echo Die Messung wurde beendet.
pause
exit /b 0

:kein_python
echo.
echo Der Ordner "python" fehlt.
echo Bitte das ZIP vollstaendig entpacken - diese Datei laesst sich nicht
echo direkt aus dem ZIP heraus starten.
echo.
pause
exit /b 1
"""

LIESMICH = """Pegellotse — portable Version

Starten:  "Pegellotse starten.bat" doppelklicken.

Der komplette Ordner muss entpackt sein. Direkt aus dem ZIP heraus startet es
nicht. Ein Python muss nicht installiert sein, es liegt im Unterordner
"python" bereits bei.

Beim ersten Start fragt die Windows-Firewall nach. Die Freigabe ist nur noetig,
wenn die Anzeige auch auf Tablet oder Handy laufen soll.

Alles Weitere steht in README.md.
"""


def lade(url: str, ziel: Path) -> None:
    print(f"  lade {url}")
    with urllib.request.urlopen(url) as antwort, open(ziel, "wb") as datei:
        shutil.copyfileobj(antwort, datei)


def pruefe(python_exe: Path, arbeitsverzeichnis: Path, argumente: list[str],
           beschreibung: str) -> None:
    """Fuehrt einen Pruefschritt aus und zeigt bei einem Fehler auch, woran es lag."""
    lauf = subprocess.run([str(python_exe), *argumente], cwd=arbeitsverzeichnis,
                          capture_output=True, text=True)
    if lauf.returncode != 0:
        print(f"\n  FEHLGESCHLAGEN: {beschreibung}")
        if lauf.stdout.strip():
            print("  Ausgabe:\n" + "\n".join("    " + z for z in lauf.stdout.splitlines()))
        if lauf.stderr.strip():
            print("  Fehler:\n" + "\n".join("    " + z for z in lauf.stderr.splitlines()))
        raise subprocess.CalledProcessError(lauf.returncode, argumente)
    print(f"  ok: {beschreibung}")
    if lauf.stdout.strip() and beschreibung.startswith("Suchpfad"):
        print("   " + lauf.stdout.strip().splitlines()[0])


def pruefe_quellen() -> None:
    """Vor dem Bauen sicherstellen, dass wirklich alles vorliegt."""
    fehlend = [n for n in DATEIEN if not (HIER / n).exists()]
    if not (HIER / "templates" / "dashboard.html").exists() \
            and not (HIER / "dashboard.html").exists():
        fehlend.append("templates/dashboard.html")
    if fehlend:
        raise FileNotFoundError(
            "Es fehlen Dateien im Quellordner:\n  "
            + "\n  ".join(fehlend)
            + f"\n\nGesucht wurde in: {HIER}")


def baue(version: str, ausgabe: Path, mit_zip: bool) -> None:
    pruefe_quellen()
    kurz = "".join(version.split(".")[:2])          # 3.12.10 -> 312
    paket = ausgabe / "Pegellotse"
    if paket.exists():
        shutil.rmtree(paket)
    (paket / "python").mkdir(parents=True)

    temp = ausgabe / "_temp"
    temp.mkdir(exist_ok=True)

    print(f"Eingebettetes Python {version} holen …")
    embed = temp / f"python-{version}-embed-amd64.zip"
    if not embed.exists():
        lade(f"https://www.python.org/ftp/python/{version}/"
             f"python-{version}-embed-amd64.zip", embed)
    with zipfile.ZipFile(embed) as z:
        z.extractall(paket / "python")

    # Das eingebettete Python legt seinen Suchpfad allein ueber die ._pth-Datei
    # fest — weder das Verzeichnis des Skripts noch site-packages kommen von
    # selbst dazu. Beides muss hier eingetragen werden, sonst findet das Paket
    # spaeter weder messkern noch numpy.
    pth = paket / "python" / f"python{kurz}._pth"
    eintraege = [f"python{kurz}.zip", ".", "..", "Lib\\site-packages", "import site"]
    pth.write_text("\n".join(eintraege) + "\n", encoding="utf-8")

    python_exe = paket / "python" / "python.exe"

    print("pip einrichten …")
    get_pip = temp / "get-pip.py"
    if not get_pip.exists():
        lade("https://bootstrap.pypa.io/get-pip.py", get_pip)
    subprocess.run([str(python_exe), str(get_pip), "--no-warn-script-location"],
                   check=True)

    print("Abhaengigkeiten installieren …")
    subprocess.run([str(python_exe), "-m", "pip", "install", "--no-warn-script-location",
                    "-r", str(HIER / "requirements.txt")], check=True)

    print("Programmdateien kopieren …")
    for name in DATEIEN:
        shutil.copy2(HIER / name, paket / name)

    # Die Weboberflaeche gehoert nach templates/. Liegt dashboard.html
    # stattdessen flach neben dem Skript — was beim Herunterladen einzelner
    # Dateien leicht passiert — wird sie hier an die richtige Stelle gelegt.
    vorlagen = HIER / "templates"
    if (vorlagen / "dashboard.html").exists():
        shutil.copytree(vorlagen, paket / "templates")
    elif (HIER / "dashboard.html").exists():
        print("  Hinweis: dashboard.html lag neben dem Skript statt in "
              "templates/ — sie wird ins Paket einsortiert.")
        (paket / "templates").mkdir()
        shutil.copy2(HIER / "dashboard.html", paket / "templates" / "dashboard.html")
    else:
        raise FileNotFoundError(
            "dashboard.html nicht gefunden — weder in templates/ noch neben "
            f"paket_bauen.py. Gesucht wurde in: {HIER}")

    for name in ORDNER:
        quelle = HIER / name
        if quelle.exists():
            shutil.copytree(quelle, paket / name)
        else:
            print(f"  Hinweis: Ordner {name}/ fehlt, wird ausgelassen.")

    (paket / "Pegellotse starten.bat").write_text(STARTER, encoding="ascii",
                                                              newline="\r\n")
    (paket / "LIESMICH.txt").write_text(LIESMICH, encoding="utf-8", newline="\r\n")

    print("Paket gegenpruefen …")
    pruefe(python_exe, paket, ["-c", "import sys; print('  Suchpfad:', sys.path)"],
           "Suchpfad des eingebetteten Python")
    pruefe(python_exe, paket, ["-c", "import numpy, scipy, flask, sounddevice"],
           "Pakete importierbar")
    pruefe(python_exe, paket, ["-c", "import messkern, pegellotse"],
           "Programmdateien importierbar")
    pruefe(python_exe, paket, ["selftest.py"], "Selbsttest der Messkette")

    groesse = sum(f.stat().st_size for f in paket.rglob("*") if f.is_file())
    print(f"\nFertig: {paket}  ({groesse/1024/1024:.0f} MB)")

    if mit_zip:
        ziel = ausgabe / "Pegellotse-portabel"
        print("ZIP packen …")
        shutil.make_archive(str(ziel), "zip", root_dir=paket.parent,
                            base_dir=paket.name)
        print(f"Fertig: {ziel}.zip "
              f"({Path(str(ziel) + '.zip').stat().st_size/1024/1024:.0f} MB)")

    shutil.rmtree(temp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Portable Version bauen")
    ap.add_argument("--python", default=STANDARD_PYTHON,
                    help=f"einzubettende Python-Version (Standard {STANDARD_PYTHON})")
    ap.add_argument("--ausgabe", default="paket", help="Zielverzeichnis")
    ap.add_argument("--zip", action="store_true", help="zusaetzlich ein ZIP packen")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("Hinweis: das eingebettete Python ist eine Windows-Version. "
              "Das Paket laesst sich nur unter Windows einrichten und testen.")

    ausgabe = Path(args.ausgabe).resolve()
    ausgabe.mkdir(parents=True, exist_ok=True)
    try:
        baue(args.python, ausgabe, args.zip)
    except FileNotFoundError as exc:
        sys.exit(f"\nAbgebrochen: {exc}")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"\nAbgebrochen: ein Schritt ist fehlgeschlagen ({exc}).")


if __name__ == "__main__":
    main()
