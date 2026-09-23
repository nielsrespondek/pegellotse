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
pegellotse.py — Pegellotse fuer Veranstaltungen.

Startet die Messung und oeffnet ein Dashboard im Browser. Alles Weitere
(Mikrofonauswahl, Kalibrierung, Protokoll, Grenzwerte) wird dort eingestellt —
die Anwendung braucht keine Kommandozeile.

config.json, das Protokollverzeichnis und eine hinterlegte Kalibrierdatei
liegen neben den Programmdateien — im portablen Paket also im selben Ordner
wie die Startdatei.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import zipfile
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from messkern import MicCorrection, SplEngine

APP_NAME = "Pegellotse"
VERSION = "1.0.0"

# Daten liegen neben der EXE bzw. neben dem Skript, Vorlagen ggf. im EXE-Bundle
# Der Dienst auf dem Pi legt seine Daten nicht neben das Programm, sondern
# dorthin, wo er auch schreiben darf.
_UMGEBUNG = os.environ.get("PEGELLOTSE_DATA")

if getattr(sys, "frozen", False):
    DATA_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", DATA_DIR))
else:
    DATA_DIR = Path(__file__).resolve().parent
    BUNDLE_DIR = DATA_DIR

if _UMGEBUNG:
    DATA_DIR = Path(_UMGEBUNG).expanduser().resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
LOG_DIR = DATA_DIR / "logs"
CALFILE_PATH = DATA_DIR / "mikrofon-kalibrierung.txt"

BLOCKSIZE = 2400          # 50 ms — bestimmt, wie oft die Anzeige nachgefuehrt wird

DEFAULTS = {
    "device": None,           # None = Standardaufnahmegeraet des Systems
    "device_name": "",        # zum Wiederfinden, falls sich die Indizes verschieben
    "samplerate": 48000,
    "channel": 0,
    "calibration_db": 120.0,
    "calibration_time": "",
    "calibration_file": "",   # Dateiname der geladenen Mikrofon-Kalibrierdatei
    "log_enabled": False,
    "rta_tau": 0.125,         # Zeitbewertung der Terzanzeige: 0.125 = Fast
    "limits": {"laeq30": 99.0, "lcpeak": 135.0, "ziel": 95.0},
    "ort": "Messplatz",
    "veranstaltung": "",
}


# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("config.json ist beschaedigt — es gelten die Standardwerte.")
            return cfg
        for key, value in stored.items():
            if key == "limits" and isinstance(value, dict):
                cfg["limits"].update(value)
            elif key in cfg:
                cfg[key] = value
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    except OSError as exc:
        print(f"config.json konnte nicht geschrieben werden: {exc}")


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------
def input_devices(neu_einlesen: bool = False) -> list[dict]:
    """
    Alle Aufnahmegeraete mit Index, Name und Schnittstelle.

    PortAudio liest die Geraeteliste beim Start einmal ein. Ein Mikrofon, das
    erst danach eingesteckt wird, taucht ohne neues Einlesen nicht auf — das
    ist der Grund, warum sonst nur ein Neustart hilft. Neu eingelesen wird nur,
    wenn gerade keine Aufnahme laeuft: das Zuruecksetzen wuerde einen offenen
    Datenstrom abreissen.
    """
    try:
        import sounddevice as sd
        if neu_einlesen:
            try:
                sd._terminate()
                sd._initialize()
            except Exception as exc:
                print(f"Geraeteliste nicht neu einlesbar: {exc}")
        apis = sd.query_hostapis()
        out = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                out.append({
                    "index": i,
                    "name": dev["name"],
                    "api": apis[dev["hostapi"]]["name"],
                    "channels": dev["max_input_channels"],
                    "samplerate": int(dev["default_samplerate"]),
                })
        return out
    except Exception as exc:
        print(f"Geraeteliste nicht lesbar: {exc}")
        return []


def lokale_adressen() -> list[str]:
    """
    IPv4-Adressen dieses Rechners im Netz — damit man die Adresse fuers Tablet
    nicht erst mit ipconfig heraussuchen muss.
    """
    adressen: list[str] = []
    try:
        # Verbindet nichts, ermittelt nur, ueber welche Adresse hinausgeroutet wird
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("192.0.2.1", 80))
            adressen.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for eintrag in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            adresse = eintrag[4][0]
            if not adresse.startswith("127.") and adresse not in adressen:
                adressen.append(adresse)
    except OSError:
        pass
    return adressen



# --------------------------------------------------------------------------
# System (nur Linux/Raspberry Pi)
# --------------------------------------------------------------------------
LINUX = sys.platform != "win32"


def _nmcli(*argumente: str, mit_sudo: bool = False, timeout: float = 20.0):
    """Ruft nmcli auf. Gibt (erfolg, ausgabe) zurueck."""
    if not shutil.which("nmcli"):
        return False, "NetworkManager (nmcli) ist nicht vorhanden."
    befehl = (["sudo", "-n", "/usr/bin/nmcli"] if mit_sudo else ["nmcli"]) + list(argumente)
    try:
        lauf = subprocess.run(befehl, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if lauf.returncode != 0:
        return False, (lauf.stderr or lauf.stdout).strip()
    return True, lauf.stdout


def cpu_temperatur() -> float | None:
    try:
        roh = Path("/sys/class/thermal/thermal_zone0/temp").read_text()
        return round(int(roh.strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def gedrosselt() -> dict | None:
    """
    Fragt den Raspberry Pi, ob er wegen Hitze oder schwacher Stromversorgung
    heruntertaktet — beides fuehrt zu Aussetzern in der Aufnahme.
    """
    if not shutil.which("vcgencmd"):
        return None
    try:
        lauf = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                              text=True, timeout=5)
        wert = int(lauf.stdout.strip().split("=")[1], 16)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {
        "unterspannung": bool(wert & 0x1),
        "gedrosselt": bool(wert & 0x4),
        "hitze": bool(wert & 0x8),
        "unterspannung_gewesen": bool(wert & 0x10000),
        "hitze_gewesen": bool(wert & 0x80000),
    }


WLAN_SKRIPT = BUNDLE_DIR / "wlan.sh"
WLAN_SCAN = DATA_DIR / "wlan-scan.txt"          # zuletzt gesehene Netze
WLAN_WECHSEL = DATA_DIR / "wlan-wechsel.txt"    # Ergebnis des letzten Netzwechsels
HOTSPOT = "hotspot"


def _felder(zeile: str) -> list[str]:
    """Zerlegt eine nmcli-Zeile im -t-Format. Doppelpunkte in Namen kommen als \\: an."""
    felder, aktuell, i = [], [], 0
    while i < len(zeile):
        z = zeile[i]
        if z == "\\" and i + 1 < len(zeile):
            aktuell.append(zeile[i + 1]); i += 2; continue
        if z == ":":
            felder.append("".join(aktuell)); aktuell = []
        else:
            aktuell.append(z)
        i += 1
    felder.append("".join(aktuell))
    return felder


def _wlan_skript(*argumente: str, timeout: float = 20.0) -> tuple[bool, str]:
    """Ruft wlan.sh mit root-Rechten auf (eine enge sudo-Regel erlaubt genau das)."""
    if not WLAN_SKRIPT.exists():
        return False, "wlan.sh fehlt — bitte install.sh erneut ausführen."
    try:
        lauf = subprocess.run(["sudo", "-n", str(WLAN_SKRIPT), *argumente],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if lauf.returncode != 0:
        return False, (lauf.stderr or lauf.stdout).strip() or "wlan.sh meldet einen Fehler."
    return True, lauf.stdout.strip()


def wlan_geraete() -> list[dict]:
    """WLAN-Chips: eingebaut und ggf. USB-Stick."""
    ok, ausgabe = _nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
    geraete = []
    if not ok:
        return geraete
    for zeile in ausgabe.splitlines():
        f = _felder(zeile)
        if len(f) >= 4 and f[1] == "wifi":
            pfad = Path("/sys/class/net") / f[0] / "device"
            try:
                usb = "/usb" in str(pfad.resolve())
            except OSError:
                usb = False
            geraete.append({"geraet": f[0], "zustand": f[2],
                            "verbindung": "" if f[3] in ("", "--") else f[3],
                            "usb": usb})
    return geraete


def wlan_lage() -> dict:
    """Womit ist der Rechner verbunden, und wie viele WLAN-Chips hat er?"""
    lage = {"verbunden": None, "signal": None, "verfuegbar": bool(shutil.which("nmcli")),
            "chips": 0, "zwei_chips": False, "hotspot_geraet": None,
            "client_geraet": None, "live_suche": False}
    if not lage["verfuegbar"]:
        return lage
    geraete = wlan_geraete()
    lage["chips"] = len(geraete)
    lage["zwei_chips"] = len(geraete) >= 2
    ap = next((g for g in geraete if g["verbindung"] == HOTSPOT), None)
    lage["hotspot_geraet"] = ap["geraet"] if ap else None
    andere = [g for g in geraete if g is not ap]
    # Gesucht wird auf allen Chips, die nicht gerade den Zugangspunkt machen.
    # Ein freier Chip sieht deutlich mehr als einer, der gerade in einem Netz
    # haengt — der bringt oft nur sein eigenes Netz zurueck.
    lage["such_geraete"] = [g["geraet"] for g in andere]
    lage["live_suche"] = bool(andere)
    client = next((g for g in andere if g["verbindung"]), None) or (andere[0] if andere else None)
    if client:
        lage["client_geraet"] = client["geraet"]
        if client["verbindung"]:
            lage["verbunden"] = client["verbindung"]
            ok, ausgabe = _nmcli("-t", "-f", "ACTIVE,SIGNAL", "device", "wifi", "list",
                                 "ifname", client["geraet"], "--rescan", "no")
            if ok:
                for zeile in ausgabe.splitlines():
                    f = _felder(zeile)
                    if len(f) >= 2 and f[0] == "yes" and f[1].isdigit():
                        lage["signal"] = int(f[1])
                        break
    return lage


def _netze_lesen(ausgabe: str) -> list[dict]:
    # Mehrere Chips sehen dasselbe Netz unterschiedlich stark, und ein Netz mit
    # mehreren Zugangspunkten steht ohnehin mehrfach in der Liste. Es zaehlt
    # jeweils das staerkste.
    beste: dict[str, dict] = {}
    for zeile in ausgabe.splitlines():
        f = _felder(zeile)
        if not f or not f[0] or f[0] == "--":
            continue
        netz = {
            "ssid": f[0],
            "signal": int(f[1]) if len(f) > 1 and f[1].isdigit() else None,
            "gesichert": bool(len(f) > 2 and f[2].strip() not in ("", "--")),
        }
        alt = beste.get(f[0])
        if alt is None or (netz["signal"] or 0) > (alt["signal"] or 0):
            beste[f[0]] = netz
    return sorted(beste.values(), key=lambda n: -(n["signal"] or 0))


def _scan_merken(ausgabe: str) -> None:
    try:
        tmp = WLAN_SCAN.with_suffix(".tmp")
        tmp.write_text(ausgabe, encoding="utf-8")
        os.replace(tmp, WLAN_SCAN)     # ersetzt auch eine Datei, die root angelegt hat
    except OSError:
        pass


def wlan_wechsel_status() -> dict | None:
    try:
        zustand, ziel, zeit, text = WLAN_WECHSEL.read_text(encoding="utf-8").strip().split("|", 3)
        return {"zustand": zustand, "ziel": ziel, "vor": int(time.time() - int(zeit)),
                "text": text}
    except (OSError, ValueError):
        return None


def wlan_netze() -> dict:
    """
    Gespeicherte und gefundene Netze. Ist ein Chip frei, wird frisch gesucht.
    Macht der einzige Chip gerade den Zugangspunkt, kann er nicht suchen —
    dann kommt die Liste, die vor dem Start des Zugangspunkts gemerkt wurde.
    """
    gespeichert = []
    ok, ausgabe = _nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show")
    if ok:
        for zeile in ausgabe.splitlines():
            f = _felder(zeile)
            if len(f) >= 3 and "wireless" in f[1] and f[0] != HOTSPOT:
                gespeichert.append({"name": f[0], "auto": f[2] == "yes"})

    lage = wlan_lage()
    live, gefunden, roh = False, [], []
    for geraet in lage.get("such_geraete", []):
        ok, ausgabe = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                             "ifname", geraet, "--rescan", "yes", timeout=30)
        if ok and ausgabe.strip():
            roh.append(ausgabe)
    if roh:
        zusammen = "\n".join(roh)
        live, gefunden = True, _netze_lesen(zusammen)
        _scan_merken(zusammen)
    stand = None
    if not live:
        try:
            gefunden = _netze_lesen(WLAN_SCAN.read_text(encoding="utf-8"))
            stand = int(time.time() - WLAN_SCAN.stat().st_mtime)
        except OSError:
            gefunden = []
    return {"gespeichert": gespeichert, "gefunden": gefunden, "live": live,
            "stand": stand, "lage": lage, "wechsel": wlan_wechsel_status()}


def hotspot_lage() -> dict:
    """Laeuft der Notfall-Zugangspunkt gerade?"""
    ok, ausgabe = _nmcli("-t", "-f", "NAME", "connection", "show", "--active")
    aktiv = ok and any(_felder(z)[0] == HOTSPOT for z in ausgabe.splitlines())
    ok2, ausgabe2 = _nmcli("-t", "-f", "NAME", "connection", "show")
    vorhanden = ok2 and any(_felder(z)[0] == HOTSPOT for z in ausgabe2.splitlines())
    return {"vorhanden": vorhanden, "aktiv": aktiv}


def hotspot_schalten(an: bool, port: int = 80) -> tuple[bool, str]:
    anhang = "" if port == 80 else f":{port}"
    if an:
        ok, ausgabe = _wlan_skript("hotspot-an")
        return ok, (f"Zugangspunkt wird gestartet — erreichbar unter http://10.42.0.1{anhang}"
                    if ok else ausgabe)
    ok, ausgabe = _wlan_skript("hotspot-aus", timeout=45)
    return ok, ("Zugangspunkt beendet." if ok else ausgabe)


def wlan_speichern(ssid: str, passwort: str, versteckt: bool = False) -> tuple[bool, str]:
    """
    Legt ein WLAN an oder erneuert das Kennwort eines vorhandenen. Aktiviert
    wird es hier nicht — das erledigt wlan_verbinden mit Rueckfallnetz.
    """
    if not ssid:
        return False, "Ohne Netzwerknamen geht es nicht."
    if ssid == HOTSPOT:
        return False, "Dieser Name ist für den Zugangspunkt reserviert."
    if passwort and not 8 <= len(passwort) <= 63:
        return False, "Ein WLAN-Kennwort hat 8 bis 63 Zeichen."
    ok, ausgabe = _nmcli("-t", "-f", "NAME", "connection", "show")
    vorhanden = ok and any(_felder(z)[0] == ssid for z in ausgabe.splitlines())

    sicherheit = (["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", passwort]
                  if passwort else [])
    if vorhanden:
        # Ohne Kennwort bleibt die bisherige Absicherung stehen.
        argumente = ["connection", "modify", ssid, "802-11-wireless.ssid", ssid,
                     "802-11-wireless.hidden", "yes" if versteckt else "no"] + sicherheit
        ok, ausgabe = _nmcli(*argumente, mit_sudo=True)
        return ok, (f"{ssid} aktualisiert.") if ok else ausgabe

    argumente = ["connection", "add", "type", "wifi", "con-name", ssid,
                 "ssid", ssid, "autoconnect", "yes",
                 "802-11-wireless.hidden", "yes" if versteckt else "no"] + sicherheit
    ok, ausgabe = _nmcli(*argumente, mit_sudo=True)
    return ok, (f"{ssid} gespeichert.") if ok else ausgabe


def wlan_verbinden(name: str, port: int = 80) -> tuple[bool, str]:
    """Wechsel ueber wlan.sh — laeuft abgekoppelt und faellt bei Misserfolg zurueck."""
    if not name:
        return False, "Welches Netz?"
    lage = wlan_lage()
    ok, ausgabe = _wlan_skript("wechsel", name)
    if not ok:
        return False, ausgabe
    rechner = socket.gethostname()
    anhang = "" if port == 80 else f":{port}"
    if lage["zwei_chips"] and lage["hotspot_geraet"]:
        return True, (f"Verbinde mit {name} … der Zugangspunkt bleibt dabei an.")
    if lage["hotspot_geraet"]:
        return True, (f"Der Zugangspunkt geht jetzt aus. Klappt es, ist der Pegellotse im Netz "
                      f"„{name}“ unter http://{rechner}.local{anhang} erreichbar. Klappt es "
                      "nicht (etwa falsches Kennwort), ist der Zugangspunkt nach etwa einer "
                      "Minute wieder da — unter Einstellungen → WLAN steht dann der Grund.")
    return True, (f"Wechsel zu {name}. Diese Seite verliert kurz die Verbindung. Klappt es "
                  f"nicht, kehrt der Rechner ins bisherige Netz zurück.")


def wlan_suchen() -> tuple[bool, str]:
    ok, ausgabe = _wlan_skript("suchen")
    return ok, ("Der Zugangspunkt ist für etwa 15 Sekunden weg. Danach wieder mit "
                "„Pegellotse“ verbinden und die Liste auffrischen." if ok else ausgabe)


def wlan_entfernen(name: str) -> tuple[bool, str]:
    if name == HOTSPOT:
        return False, "Der Zugangspunkt wird nicht hier entfernt."
    ok, ausgabe = _nmcli("connection", "delete", name, mit_sudo=True)
    return ok, (f"{name} entfernt.") if ok else ausgabe


# --------------------------------------------------------------------------
# Uhrzeit
# --------------------------------------------------------------------------
# Ein Raspberry Pi hat keine batteriegepufferte Uhr. Ohne Netzwerkzeit stehen
# im Protokoll falsche Zeitstempel und die halbstuendigen Bloecke liegen
# daneben. Weil ohnehin ein Tablet oder Handy auf das Dashboard schaut, kann
# die Uhrzeit von dort kommen.
def kann_uhr_stellen() -> bool:
    """Unter Windows nie, unter Linux nur mit timedatectl."""
    return sys.platform != "win32" and shutil.which("timedatectl") is not None


def systemuhr_stellen(zeitstempel: float) -> tuple[bool, str]:
    """Stellt die Systemuhr. Braucht Rechte, die install.sh gezielt einraeumt."""
    if sys.platform == "win32":
        return False, "Unter Windows wird die Uhr nicht vom Programm gestellt."
    stempel = datetime.fromtimestamp(zeitstempel).strftime("%Y-%m-%d %H:%M:%S")
    befehle = [
        ["sudo", "-n", "/usr/bin/timedatectl", "set-time", stempel],
        ["/usr/bin/timedatectl", "set-time", stempel],
    ]
    letzter = ""
    for befehl in befehle:
        try:
            lauf = subprocess.run(befehl, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            letzter = str(exc)
            continue
        if lauf.returncode == 0:
            return True, f"Uhr gestellt: {stempel}"
        letzter = (lauf.stderr or lauf.stdout).strip()
    return False, f"Uhr konnte nicht gestellt werden: {letzter}"


# --------------------------------------------------------------------------
# Messlauf
# --------------------------------------------------------------------------
class Monitor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.q: queue.Queue = queue.Queue(maxsize=64)
        self.stream = None
        self.device_label = ""
        self.fehler = ""
        self.dropped = 0
        self.adressen: list[str] = []
        self.port = 8000
        self.letzter_block = 0.0       # wann kam zuletzt Audio an
        self.letztes_signal = 0.0      # wann war zuletzt etwas zu hoeren
        self.neustarts = 0             # wie oft die Aufnahme wiederbelebt wurde
        self.wache_meldung = ""
        self.markierung_offen = ""

        self.csv_file = None
        self.csv_writer = None
        self.csv_name = ""
        self._last_log = 0.0

        # Kalibriervorgang
        self.cal_active = False
        self.cal_until = 0.0
        self.cal_ref = 94.0
        self._cal_sum = 0.0
        self._cal_n = 0
        self.cal_result: dict | None = None

        self.engine = self._new_engine()
        self._stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._wache, daemon=True).start()

    # -- Messkern ----------------------------------------------------------
    def _new_engine(self) -> SplEngine:
        engine = SplEngine(
            fs=self.cfg["samplerate"],
            calibration_db=self.cfg["calibration_db"],
            windows=(10.0, 60.0, 300.0, 1800.0, 3600.0),
            block_length=1800.0,
        )
        engine.spektrum_tau = float(self.cfg.get("rta_tau", 0.125))
        self._apply_calfile(engine)
        return engine

    def _apply_calfile(self, engine: SplEngine) -> None:
        if not self.cfg.get("calibration_file") or not CALFILE_PATH.exists():
            engine.correction = None
            engine.correction_info = None
            return
        try:
            corr, info = MicCorrection.from_file(
                CALFILE_PATH.read_text(encoding="utf-8", errors="replace"),
                self.cfg["samplerate"])
            engine.correction = corr
            engine.correction_info = {**info, "datei": self.cfg["calibration_file"]}
        except Exception as exc:
            print(f"Kalibrierdatei nicht lesbar: {exc}")
            self.cfg["calibration_file"] = ""

    # -- Aufnahme ----------------------------------------------------------
    def _callback(self, indata, frames, time_info, status):
        if status:
            self.dropped += 1
        try:
            channel = min(self.cfg["channel"], indata.shape[1] - 1)
            self.q.put_nowait((time.monotonic(), indata[:, channel].copy()))
        except queue.Full:
            self.dropped += 1

    def start_stream(self) -> bool:
        """Startet die Aufnahme neu. Gibt False zurueck, wenn es nicht klappt."""
        self.stop_stream()
        try:
            import sounddevice as sd
            dev = self.cfg["device"]
            info = sd.query_devices(dev if dev is not None else None, "input")
            channels = max(1, min(info["max_input_channels"], self.cfg["channel"] + 1))
            with self.lock:
                self.engine = self._new_engine()
            self.stream = sd.InputStream(
                device=dev,
                channels=channels,
                samplerate=self.cfg["samplerate"],
                blocksize=BLOCKSIZE,
                dtype="float32",
                callback=self._callback,
            )
            self.stream.start()
            self.device_label = str(info["name"])
            self.cfg["device_name"] = str(info["name"])
            self.fehler = ""
            self.dropped = 0
            print(f"Aufnahme laeuft: {self.device_label}")
            return True
        except Exception as exc:
            self.stream = None
            self.device_label = ""
            self.fehler = str(exc)
            print(f"Aufnahme nicht gestartet: {exc}")
            return False

    def stop_stream(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # -- Protokoll ---------------------------------------------------------
    def set_logging(self, enabled: bool) -> None:
        with self.lock:
            self.cfg["log_enabled"] = bool(enabled)
            if not enabled:
                self._close_log()
        save_config(self.cfg)

    def _close_log(self) -> None:
        if self.csv_file:
            self.csv_file.close()
        self.csv_file = None
        self.csv_writer = None
        self.csv_name = ""

    def _open_log(self) -> None:
        LOG_DIR.mkdir(exist_ok=True)
        stem = datetime.now().strftime("%Y-%m-%d_%H-%M")
        if self.cfg["veranstaltung"]:
            safe = "".join(c for c in self.cfg["veranstaltung"]
                           if c.isalnum() or c in " -_").strip().replace(" ", "-")
            if safe:
                stem = f"{stem}_{safe[:40]}"
        path = LOG_DIR / f"{stem}.csv"
        self.csv_file = open(path, "w", newline="", encoding="utf-8-sig")
        self.csv_writer = csv.writer(self.csv_file, delimiter=";")
        self.csv_writer.writerow([f"# {APP_NAME} {VERSION}",
                                  f"Offset {self.cfg['calibration_db']:.2f} dB",
                                  f"Mikrofon {self.cfg['device_name']}",
                                  f"Ort {self.cfg['ort']}"])
        self.csv_writer.writerow([
            "Zeit", "LAF", "LAeq_1s", "LAeq_10s", "LAeq_1min", "LAeq_5min",
            "LAeq_30min", "LAeq_60min", "LCpeak", "Block_LAeq", "Uebersteuert",
            "Bemerkung",
        ])
        self.csv_name = path.name
        print(f"Protokoll: {path}")

    def _write_log(self, t_end: float, result: dict) -> None:
        if self.csv_writer is None:
            self._open_log()
        e = self.engine
        cal = e.calibration_db

        def fmt(v):
            return f"{v:.1f}".replace(".", ",") if v is not None else ""

        self.csv_writer.writerow([
            datetime.fromtimestamp(e.wanduhr(t_end)).strftime("%Y-%m-%d %H:%M:%S"),
            fmt(e.laf), fmt(result["laeq_block"]), fmt(e.windows[10].value(cal)),
            fmt(e.windows[60].value(cal)), fmt(e.windows[300].value(cal)),
            fmt(e.windows[1800].value(cal)), fmt(e.windows[3600].value(cal)),
            fmt(result["lcpeak"]), fmt(e.block.leq(cal)),
            "ja" if e.clipping else "",
            self.markierung_offen,
        ])
        self.markierung_offen = ""
        self.csv_file.flush()

    # -- Kalibrierung ------------------------------------------------------
    def start_calibration(self, reference_db: float, seconds: float = 5.0) -> None:
        with self.lock:
            self.cal_ref = float(reference_db)
            self.cal_until = time.monotonic() + seconds
            self._cal_sum, self._cal_n = 0.0, 0
            self.cal_active = True
            self.cal_result = None

    def _finish_calibration(self) -> None:
        self.cal_active = False
        if self._cal_n == 0:
            self.cal_result = {"ok": False,
                               "text": "Kein Signal empfangen — Mikrofonauswahl prüfen."}
            return
        rms = math.sqrt(self._cal_sum / self._cal_n)
        if rms < 1e-6:
            self.cal_result = {"ok": False,
                               "text": "Der Eingang war praktisch still. Sitzt der "
                                       "Kalibrator richtig auf und ist er eingeschaltet?"}
            return
        offset = self.cal_ref - 20.0 * math.log10(rms)
        dbfs = 20.0 * math.log10(rms)
        hinweis = ""
        if dbfs > -6:
            hinweis = ("Der Eingangspegel ist sehr hoch — bei lauten Passagen droht "
                       "Übersteuerung. Aufnahmepegel senken und neu einmessen.")
        elif dbfs < -40:
            hinweis = ("Der Eingangspegel ist sehr niedrig — Aufnahmepegel anheben "
                       "und neu einmessen.")
        self.cfg["calibration_db"] = round(offset, 2)
        self.cfg["calibration_time"] = datetime.now().strftime("%d.%m.%Y %H:%M")
        self.engine.set_calibration(self.cfg["calibration_db"])
        save_config(self.cfg)
        self.cal_result = {
            "ok": True,
            "offset": self.cfg["calibration_db"],
            "dbfs": round(dbfs, 1),
            "text": (f"Eingemessen: Offset {self.cfg['calibration_db']:.2f} dB "
                     f"(Kalibratorsignal bei {dbfs:.1f} dBFS). {hinweis}").strip(),
        }

    # -- Verarbeitung ------------------------------------------------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                t_end, samples = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            self.letzter_block = time.monotonic()
            with self.lock:
                result = self.engine.process(samples, t_end=t_end)
                # Digitale Stille erkennen — unabhaengig von der Kalibrierung
                if result["rms_raw"] > 1e-5:
                    self.letztes_signal = self.letzter_block

                if self.cal_active:
                    self._cal_sum += result["rms_raw"] ** 2
                    self._cal_n += 1
                    if t_end >= self.cal_until:
                        self._finish_calibration()

                if self.cfg["log_enabled"]:
                    if t_end - self._last_log >= 1.0:
                        self._last_log = t_end
                        self._write_log(t_end, result)
                elif self.csv_writer is not None:
                    self._close_log()

    # -- Waechter ----------------------------------------------------------
    def _wache(self) -> None:
        """
        Prueft laufend, ob noch Audio ankommt, und startet die Aufnahme sonst
        neu. Ohne das bliebe ein herausgerutschtes USB-Kabel bis zum Ende der
        Veranstaltung unbemerkt, wenn gerade niemand aufs Dashboard schaut.
        """
        while not self._stop.wait(5.0):
            jetzt = time.monotonic()
            steht = self.stream is None or (self.letzter_block > 0
                                            and jetzt - self.letzter_block > 5.0)
            if not steht:
                if self.wache_meldung:
                    self.wache_meldung = ""
                continue
            self.wache_meldung = ("Es kam kein Ton mehr an — Aufnahme wird neu "
                                  "gestartet.")
            print(f"Waechter: keine Audiodaten seit "
                  f"{jetzt - self.letzter_block:.0f} s, starte Aufnahme neu")
            if self.start_stream():
                self.neustarts += 1
                self.letzter_block = time.monotonic()
                self.wache_meldung = (f"Aufnahme wurde neu gestartet "
                                      f"({self.neustarts}. Mal).")
            else:
                self.wache_meldung = (f"Aufnahme laesst sich nicht starten: "
                                      f"{self.fehler}")

    # -- Markierungen ------------------------------------------------------
    def markieren(self, text: str) -> tuple[bool, str]:
        if not self.cfg["log_enabled"]:
            return False, "Es läuft kein Protokoll."
        self.markierung_offen = text.strip()[:120] or "Markierung"
        return True, f"Markierung „{self.markierung_offen}“ wird eingetragen."

    # -- Zustand fuer die Oberflaeche --------------------------------------
    def state(self, mit_verlauf: bool = True) -> dict:
        with self.lock:
            snap = self.engine.snapshot(self.cfg["limits"])
            hist = list(self.engine.history) if mit_verlauf else []
            corr = self.engine.correction_info
        jetzt = time.monotonic()
        if mit_verlauf:
            step = max(1, len(hist) // 900)
            snap["history"] = [{"t": round(t - jetzt, 1), "l": round(v, 1)}
                               for t, v in hist[::step]]
        snap.update({
            "limits": self.cfg["limits"],
            "ort": self.cfg["ort"],
            "veranstaltung": self.cfg["veranstaltung"],
            "dropped": self.dropped,
            "wache": {
                "meldung": self.wache_meldung,
                "neustarts": self.neustarts,
                "stille_s": round(jetzt - self.letztes_signal, 1)
                            if self.letztes_signal else None,
            },
            "now": self.engine.wanduhr(),
            "laeuft": self.stream is not None,
            "geraet": self.device_label,
            "fehler": self.fehler,
            "korrektur": corr,
            "kalibriert_am": self.cfg["calibration_time"],
            "log": {"aktiv": self.cfg["log_enabled"], "datei": self.csv_name},
            "kalibrierung": {
                "laeuft": self.cal_active,
                "rest": max(0.0, self.cal_until - jetzt) if self.cal_active else 0.0,
                "ergebnis": self.cal_result,
            },
            "rta_tau": self.cfg.get("rta_tau", 0.125),
            "adressen": self.adressen,
            "version": VERSION,
        })
        return snap



# --------------------------------------------------------------------------
# Protokolle
# --------------------------------------------------------------------------
def protokolle() -> list[dict]:
    """Vorhandene Protokolldateien, neueste zuerst."""
    if not LOG_DIR.exists():
        return []
    eintraege = []
    for datei in LOG_DIR.glob("*.csv"):
        try:
            angaben = datei.stat()
        except OSError:
            continue
        eintraege.append({
            "name": datei.name,
            "bytes": angaben.st_size,
            "geaendert": datetime.fromtimestamp(angaben.st_mtime).strftime("%d.%m.%Y %H:%M"),
            "zeit": angaben.st_mtime,
        })
    eintraege.sort(key=lambda e: -e["zeit"])
    return eintraege


def protokoll_pfad(name: str) -> Path | None:
    """
    Wandelt einen Dateinamen in einen Pfad um — und nur, wenn er tatsaechlich
    im Protokollverzeichnis liegt. Ohne diese Pruefung koennte ueber die
    Adresszeile jede Datei des Rechners abgerufen werden.
    """
    if not name or "/" in name or "\\" in name:
        return None
    ziel = (LOG_DIR / name).resolve()
    if ziel.parent != LOG_DIR.resolve() or ziel.suffix.lower() != ".csv":
        return None
    return ziel if ziel.is_file() else None


# --------------------------------------------------------------------------
# Weboberflaeche
# --------------------------------------------------------------------------
def vorlagenordner() -> Path:
    """
    Normalerweise liegt dashboard.html in templates/. Liegt sie flach neben
    pegellotse.py, wird sie auch dort gefunden — das erspart Suchen, wenn die
    Ordnerstruktur beim Kopieren verlorengegangen ist.
    """
    for ordner in (BUNDLE_DIR / "templates", BUNDLE_DIR):
        if (ordner / "dashboard.html").exists():
            return ordner
    return BUNDLE_DIR / "templates"


def build_app(monitor: Monitor) -> Flask:
    vorlagen = vorlagenordner()
    app = Flask(__name__, template_folder=str(vorlagen))

    @app.errorhandler(Exception)
    def zeige_fehler(exc):
        """
        Ohne diesen Handler steht im Browser nur "Internal Server Error" und
        der Grund bleibt im Verborgenen. Das Programm laeuft lokal, also darf
        die Ursache ruhig sichtbar sein — sonst sucht man im Dunkeln.
        """
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            return exc
        spur = traceback.format_exc()
        print("\n--- Fehler bei der Anfrage ---\n" + spur, flush=True)
        hinweis = ""
        if not (vorlagen / "dashboard.html").exists():
            hinweis = ("<p><b>dashboard.html wurde nicht gefunden.</b> Sie "
                       f"gehoert nach <code>{BUNDLE_DIR / 'templates'}</code> "
                       "(oder direkt neben pegellotse.py). Wurde der Ordner "
                       "vollstaendig entpackt?</p>")
        return (f"<h2>Es ist ein Fehler aufgetreten</h2>{hinweis}"
                f"<pre style='white-space:pre-wrap'>{spur}</pre>"), 500

    @app.get("/")
    def index():
        return render_template("dashboard.html", version=VERSION)

    @app.get("/api/state")
    def state():
        # ?verlauf=0 laesst die Verlaufsdaten weg — fuer die schnelle Anzeige
        mit_verlauf = request.args.get("verlauf", "1") != "0"
        return jsonify(monitor.state(mit_verlauf=mit_verlauf))

    @app.get("/api/spektrum")
    def spektrum():
        """Nur die Terzbaender — klein genug, um zehnmal je Sekunde zu laufen."""
        with monitor.lock:
            daten = monitor.engine.spektrum.snapshot(monitor.engine.calibration_db)
            laf = monitor.engine.laf
        return jsonify({"spektrum": daten, "laf": laf,
                        "tau": monitor.cfg.get("rta_tau", 0.125)})

    @app.post("/api/markierung")
    def markierung():
        data = request.get_json(force=True, silent=True) or {}
        ok, text = monitor.markieren(str(data.get("text", "")))
        return jsonify({"ok": ok, "text": text})

    @app.get("/api/logs")
    def logs():
        return jsonify({
            "dateien": protokolle(),
            "verzeichnis": str(LOG_DIR),
            "laeuft": monitor.csv_name,
        })

    @app.get("/logs/<name>")
    def log_holen(name):
        ziel = protokoll_pfad(name)
        if ziel is None:
            return "Datei nicht gefunden", 404
        return send_file(ziel, as_attachment=True, download_name=name,
                         mimetype="text/csv")

    @app.get("/logs.zip")
    def logs_zip():
        dateien = protokolle()
        if not dateien:
            return "Keine Protokolle vorhanden", 404
        puffer = io.BytesIO()
        with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as archiv:
            for eintrag in dateien:
                ziel = protokoll_pfad(eintrag["name"])
                if ziel is not None:
                    archiv.write(ziel, eintrag["name"])
        puffer.seek(0)
        stempel = datetime.now().strftime("%Y-%m-%d")
        return send_file(puffer, as_attachment=True, mimetype="application/zip",
                         download_name=f"pegellotse-protokolle-{stempel}.zip")

    @app.post("/api/logs/loeschen")
    def log_loeschen():
        data = request.get_json(force=True, silent=True) or {}
        name = str(data.get("name", ""))
        if name and name == monitor.csv_name:
            return jsonify({"fehler": "Diese Datei wird gerade beschrieben. "
                                      "Erst das Protokoll anhalten."}), 409
        ziel = protokoll_pfad(name)
        if ziel is None:
            return jsonify({"fehler": "Datei nicht gefunden."}), 404
        try:
            ziel.unlink()
        except OSError as exc:
            return jsonify({"fehler": str(exc)}), 500
        return jsonify({"ok": True, "text": f"{name} gelöscht."})

    @app.get("/api/system")
    def system():
        return jsonify({
            "linux": LINUX,
            "rechnername": socket.gethostname(),
            "adressen": monitor.adressen,
            "port": monitor.port,
            "temperatur": cpu_temperatur(),
            "drosselung": gedrosselt(),
            "wlan": wlan_lage() if LINUX else {"verfuegbar": False},
            "wlan_wechsel": wlan_wechsel_status() if LINUX else None,
            "hotspot": hotspot_lage() if LINUX else {"vorhanden": False, "aktiv": False},
        })

    @app.get("/api/wlan")
    def wlan():
        if not LINUX:
            return jsonify({"fehler": "Nur auf dem Raspberry Pi."}), 400
        return jsonify(wlan_netze())

    @app.post("/api/wlan")
    def wlan_neu():
        data = request.get_json(force=True, silent=True) or {}
        ssid = str(data.get("ssid", "")).strip()
        ok, text = wlan_speichern(ssid, str(data.get("passwort", "")),
                                  bool(data.get("versteckt")))
        if ok and data.get("verbinden"):
            ok, text2 = wlan_verbinden(ssid, monitor.port)
            text = text + " " + text2
        return jsonify({"ok": ok, "text": text})

    @app.post("/api/wlan/verbinden")
    def wlan_wechsel():
        data = request.get_json(force=True, silent=True) or {}
        ok, text = wlan_verbinden(str(data.get("name", "")), monitor.port)
        return jsonify({"ok": ok, "text": text})

    @app.post("/api/wlan/suchen")
    def wlan_neu_suchen():
        ok, text = wlan_suchen()
        return jsonify({"ok": ok, "text": text})

    @app.get("/api/wlan/wechsel")
    def wlan_wechsel_lage():
        return jsonify({"wechsel": wlan_wechsel_status(), "lage": wlan_lage()})

    @app.post("/api/wlan/entfernen")
    def wlan_weg():
        data = request.get_json(force=True, silent=True) or {}
        ok, text = wlan_entfernen(str(data.get("name", "")))
        return jsonify({"ok": ok, "text": text})

    @app.post("/api/hotspot")
    def hotspot():
        data = request.get_json(force=True, silent=True) or {}
        ok, text = hotspot_schalten(bool(data.get("an")), monitor.port)
        return jsonify({"ok": ok, "text": text})

    @app.post("/api/zeit")
    def zeit_pruefen():
        """Vergleicht die Uhr des Endgeraets mit der des Rechners."""
        data = request.get_json(force=True, silent=True) or {}
        try:
            browser = float(data["zeit"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"fehler": "Zeitangabe fehlt"}), 400
        abweichung = browser - time.time()
        return jsonify({
            "abweichung": round(abweichung, 1),
            "stellbar": kann_uhr_stellen(),
            "misst": monitor.stream is not None and monitor.cfg["log_enabled"],
        })

    @app.post("/api/zeit/stellen")
    def zeit_stellen():
        data = request.get_json(force=True, silent=True) or {}
        try:
            browser = float(data["zeit"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"fehler": "Zeitangabe fehlt"}), 400
        if monitor.cfg["log_enabled"]:
            return jsonify({"fehler": "Erst das Protokoll anhalten — ein Zeitsprung "
                                      "mitten in der Aufzeichnung macht sie unbrauchbar."}), 409
        ok, text = systemuhr_stellen(browser)
        if ok:
            with monitor.lock:
                monitor.engine.stelle_uhr()
        return jsonify({"ok": ok, "text": text})

    @app.get("/api/devices")
    def devices():
        # Nur neu einlesen, wenn keine Aufnahme laeuft — sonst risse sie ab
        return jsonify({
            "geraete": input_devices(neu_einlesen=monitor.stream is None),
            "aktiv": monitor.cfg["device"],
            "kanal": monitor.cfg["channel"],
            "samplerate": monitor.cfg["samplerate"],
        })

    @app.post("/api/device")
    def set_device():
        data = request.get_json(force=True, silent=True) or {}
        if "index" in data:
            wert = data["index"]
            monitor.cfg["device"] = None if wert in (None, "", "auto") else int(wert)
        if "kanal" in data:
            monitor.cfg["channel"] = max(0, int(data["kanal"]))
        if data.get("samplerate"):
            monitor.cfg["samplerate"] = int(data["samplerate"])
        ok = monitor.start_stream()
        save_config(monitor.cfg)
        return jsonify({"ok": ok, "geraet": monitor.device_label,
                        "fehler": monitor.fehler})

    @app.post("/api/settings")
    def settings():
        data = request.get_json(force=True, silent=True) or {}
        for key in ("ort", "veranstaltung"):
            if key in data:
                monitor.cfg[key] = str(data[key])[:80]
        for key in ("laeq30", "lcpeak", "ziel"):
            if key in data:
                try:
                    monitor.cfg["limits"][key] = float(data[key])
                except (TypeError, ValueError):
                    return jsonify({"fehler": f"{key} ist keine Zahl"}), 400
        if "rta_tau" in data:
            try:
                tau = float(data["rta_tau"])
            except (TypeError, ValueError):
                return jsonify({"fehler": "Zeitkonstante ist keine Zahl"}), 400
            monitor.cfg["rta_tau"] = max(0.01, min(2.0, tau))
            monitor.engine.spektrum_tau = monitor.cfg["rta_tau"]
        if data.get("offset") not in (None, ""):
            try:
                monitor.cfg["calibration_db"] = float(data["offset"])
            except (TypeError, ValueError):
                return jsonify({"fehler": "Offset ist keine Zahl"}), 400
            monitor.cfg["calibration_time"] = (
                datetime.now().strftime("%d.%m.%Y %H:%M") + " (von Hand)")
            monitor.engine.set_calibration(monitor.cfg["calibration_db"])
        save_config(monitor.cfg)
        return jsonify({"ok": True})

    @app.post("/api/log")
    def toggle_log():
        data = request.get_json(force=True, silent=True) or {}
        an = bool(data.get("aktiv"))
        if an:
            # Der Name wandert in den Dateinamen. Ohne ihn heissen hinterher
            # alle Protokolle gleich und niemand weiss mehr, wozu sie gehoeren.
            name = str(data.get("veranstaltung", "")).strip()[:80]
            if name:
                monitor.cfg["veranstaltung"] = name
                save_config(monitor.cfg)
            if not monitor.cfg["veranstaltung"].strip():
                return jsonify({
                    "fehler": "Bitte zuerst eintragen, um welche Veranstaltung "
                              "es geht — der Name steht später im Dateinamen.",
                    "feld": "veranstaltung",
                }), 400
        monitor.set_logging(an)
        return jsonify({"aktiv": monitor.cfg["log_enabled"],
                        "veranstaltung": monitor.cfg["veranstaltung"]})

    @app.post("/api/calibrate")
    def calibrate():
        data = request.get_json(force=True, silent=True) or {}
        if monitor.stream is None:
            return jsonify({"fehler": "Es läuft keine Aufnahme."}), 400
        try:
            ref = float(data.get("referenz", 94.0))
        except (TypeError, ValueError):
            return jsonify({"fehler": "Referenzpegel ist keine Zahl"}), 400
        monitor.start_calibration(ref, float(data.get("dauer", 5.0)))
        return jsonify({"ok": True})

    @app.post("/api/calfile")
    def calfile():
        datei = request.files.get("datei")
        if datei is None:
            return jsonify({"fehler": "Keine Datei empfangen."}), 400
        text = datei.read().decode("utf-8", errors="replace")
        try:
            MicCorrection.from_file(text, monitor.cfg["samplerate"])
        except Exception as exc:
            return jsonify({"fehler": str(exc)}), 400
        CALFILE_PATH.write_text(text, encoding="utf-8")
        monitor.cfg["calibration_file"] = datei.filename or "kalibrierung.txt"
        save_config(monitor.cfg)
        with monitor.lock:
            monitor._apply_calfile(monitor.engine)
            info = monitor.engine.correction_info
        return jsonify({"ok": True, "info": info})

    @app.post("/api/calfile/entfernen")
    def calfile_remove():
        monitor.cfg["calibration_file"] = ""
        save_config(monitor.cfg)
        with monitor.lock:
            monitor.engine.correction = None
            monitor.engine.correction_info = None
        return jsonify({"ok": True})

    @app.post("/api/reset-peaks")
    def reset_peaks():
        with monitor.lock:
            monitor.engine.laf_max = 0.0
            monitor.engine.lcpeak_max = 0.0
        return jsonify({"ok": True})

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--kein-browser", action="store_true",
                    help="Browser nicht automatisch oeffnen")
    args = ap.parse_args()

    cfg = load_config()
    monitor = Monitor(cfg)
    monitor.start_stream()   # scheitert still — die Auswahl folgt im Dashboard

    monitor.port = args.port
    monitor.adressen = lokale_adressen()
    app = build_app(monitor)
    anhang = "" if args.port == 80 else f":{args.port}"
    url = f"http://localhost{anhang}"
    print(f"\n{APP_NAME} {VERSION}")
    print(f"Auf diesem Rechner:  {url}")
    if monitor.adressen:
        print("Im selben WLAN (Tablet, Handy):")
        for adresse in monitor.adressen:
            print(f"                     http://{adresse}{anhang}")
    else:
        print("Im WLAN:             keine Netzwerkadresse gefunden — "
              "ist der Rechner verbunden?")
    print("\nDieses Fenster schliessen beendet die Messung.\n")

    if not args.kein_browser and os.environ.get("WERKZEUG_RUN_MAIN") is None:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=args.host, port=args.port, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop_stream()


if __name__ == "__main__":
    main()
