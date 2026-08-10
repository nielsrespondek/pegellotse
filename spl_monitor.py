"""
spl_monitor.py — Schallpegel-Monitoring fuer Veranstaltungen.

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
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from spl_core import MicCorrection, SplEngine

APP_NAME = "Schallpegel-Monitoring"
VERSION = "1.0.0"

# Daten liegen neben der EXE bzw. neben dem Skript, Vorlagen ggf. im EXE-Bundle
# Der Dienst auf dem Pi legt seine Daten nicht neben das Programm, sondern
# dorthin, wo er auch schreiben darf.
_UMGEBUNG = os.environ.get("SPLMON_DATA")

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
def input_devices() -> list[dict]:
    """Alle Aufnahmegeraete mit Index, Name und Schnittstelle."""
    try:
        import sounddevice as sd
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


def wlan_lage() -> dict:
    """Womit ist der Rechner gerade verbunden?"""
    lage = {"verbunden": None, "signal": None, "verfuegbar": bool(shutil.which("nmcli"))}
    ok, ausgabe = _nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi")
    if ok:
        for zeile in ausgabe.splitlines():
            teile = zeile.split(":")
            if teile and teile[0] == "yes" and len(teile) >= 3:
                lage["verbunden"] = teile[1]
                lage["signal"] = int(teile[2]) if teile[2].isdigit() else None
                break
    return lage


def wlan_netze() -> dict:
    """Gespeicherte und in Reichweite gefundene Netze."""
    gespeichert = []
    ok, ausgabe = _nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show")
    if ok:
        for zeile in ausgabe.splitlines():
            teile = zeile.split(":")
            if len(teile) >= 3 and "wireless" in teile[1]:
                gespeichert.append({"name": teile[0], "auto": teile[2] == "yes"})

    gefunden = []
    ok, ausgabe = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                         "--rescan", "yes", timeout=30)
    if ok:
        gesehen = set()
        for zeile in ausgabe.splitlines():
            teile = zeile.split(":")
            if not teile or not teile[0] or teile[0] in gesehen:
                continue
            gesehen.add(teile[0])
            gefunden.append({
                "ssid": teile[0],
                "signal": int(teile[1]) if len(teile) > 1 and teile[1].isdigit() else None,
                "gesichert": bool(len(teile) > 2 and teile[2].strip()),
            })
        gefunden.sort(key=lambda n: -(n["signal"] or 0))
    return {"gespeichert": gespeichert, "gefunden": gefunden, "lage": wlan_lage()}


def hotspot_lage() -> dict:
    """Laeuft der Notfall-Zugangspunkt gerade?"""
    ok, ausgabe = _nmcli("-t", "-f", "NAME", "connection", "show", "--active")
    aktiv = ok and any(z.strip() == "hotspot" for z in ausgabe.splitlines())
    ok2, ausgabe2 = _nmcli("-t", "-f", "NAME", "connection", "show")
    vorhanden = ok2 and any(z.strip() == "hotspot" for z in ausgabe2.splitlines())
    return {"vorhanden": vorhanden, "aktiv": aktiv}


def hotspot_schalten(an: bool) -> tuple[bool, str]:
    if an:
        ok, ausgabe = _nmcli("connection", "up", "hotspot", mit_sudo=True, timeout=45)
        return ok, ("Zugangspunkt läuft — erreichbar unter http://10.42.0.1:8000"
                    if ok else ausgabe)
    ok, ausgabe = _nmcli("connection", "down", "hotspot", mit_sudo=True, timeout=45)
    return ok, ("Zugangspunkt beendet. Der Rechner sucht jetzt wieder nach "
                "bekannten Netzen." if ok else ausgabe)


def wlan_speichern(ssid: str, passwort: str, sofort: bool) -> tuple[bool, str]:
    """
    Legt ein WLAN an. Standardmaessig wird es nur gespeichert und nicht sofort
    aktiviert — ein Netzwechsel mitten im Betrieb wuerde die gerade offene
    Verbindung zum Dashboard abreissen lassen.
    """
    if not ssid:
        return False, "Ohne Netzwerknamen geht es nicht."
    if sofort:
        argumente = ["device", "wifi", "connect", ssid]
        if passwort:
            argumente += ["password", passwort]
        ok, ausgabe = _nmcli(*argumente, mit_sudo=True, timeout=45)
        return ok, ("Verbunden mit " + ssid) if ok else ausgabe

    argumente = ["connection", "add", "type", "wifi", "con-name", ssid,
                 "ssid", ssid, "autoconnect", "yes"]
    if passwort:
        argumente += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", passwort]
    ok, ausgabe = _nmcli(*argumente, mit_sudo=True)
    if not ok and "already exists" in ausgabe:
        return False, f"Ein Eintrag namens {ssid} ist bereits vorhanden."
    return ok, (f"{ssid} gespeichert — der Rechner verbindet sich, sobald das Netz "
                "in Reichweite ist.") if ok else ausgabe


def wlan_entfernen(name: str) -> tuple[bool, str]:
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
        ])
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
            with self.lock:
                result = self.engine.process(samples, t_end=t_end)

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
# Weboberflaeche
# --------------------------------------------------------------------------
def vorlagenordner() -> Path:
    """
    Normalerweise liegt dashboard.html in templates/. Liegt sie flach neben
    spl_monitor.py, wird sie auch dort gefunden — das erspart Suchen, wenn die
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
                       "(oder direkt neben spl_monitor.py). Wurde der Ordner "
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
        ok, text = wlan_speichern(str(data.get("ssid", "")).strip(),
                                  str(data.get("passwort", "")),
                                  bool(data.get("sofort")))
        return jsonify({"ok": ok, "text": text})

    @app.post("/api/wlan/entfernen")
    def wlan_weg():
        data = request.get_json(force=True, silent=True) or {}
        ok, text = wlan_entfernen(str(data.get("name", "")))
        return jsonify({"ok": ok, "text": text})

    @app.post("/api/hotspot")
    def hotspot():
        data = request.get_json(force=True, silent=True) or {}
        ok, text = hotspot_schalten(bool(data.get("an")))
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
        return jsonify({
            "geraete": input_devices(),
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
        monitor.set_logging(bool(data.get("aktiv")))
        return jsonify({"aktiv": monitor.cfg["log_enabled"]})

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
    url = f"http://localhost:{args.port}"
    print(f"\n{APP_NAME} {VERSION}")
    print(f"Auf diesem Rechner:  {url}")
    if monitor.adressen:
        print("Im selben WLAN (Tablet, Handy):")
        for adresse in monitor.adressen:
            print(f"                     http://{adresse}:{args.port}")
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
