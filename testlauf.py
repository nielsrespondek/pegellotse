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

"""Probelauf ohne Mikrofon: speist ein synthetisches Signal ein und prueft die API."""

import json
import math
import threading
import time

import numpy as np

import pegellotse as sm


def main():
    cfg = sm.load_config()
    cfg["calibration_db"] = 94.0
    cfg["veranstaltung"] = "Probelauf"
    mon = sm.Monitor(cfg)

    fs = cfg["samplerate"]
    t = np.arange(4800) / fs
    phase = 0.0
    now = time.time() - 2400  # so tun, als liefe die Messung seit 40 Minuten
    rng = np.random.default_rng(1)

    for step in range(24000):  # 2400 s in 100-ms-Blöcken
        level_db = 88 + 6 * math.sin(step / 900) + rng.normal(0, 1.5)
        amp = 10 ** ((level_db - 94) / 20) * math.sqrt(2)
        block = amp * np.sin(2 * np.pi * 1000 * t + phase)
        phase = (phase + 2 * np.pi * 1000 * 0.1) % (2 * np.pi)
        now += 0.1
        result = mon.engine.process(block, t_end=now)

    state = mon.state()
    print("Fenster:")
    for sek, w in state["windows"].items():
        v = w["value"]
        print(f"  {int(sek):>5} s   {v:.1f} dB(A)   Fenster zu {w['filled']*100:.0f} % gefuellt")
    b = state["block"]
    print(f"\nBlock: Leq {b['leq']:.1f} dB(A), noch {b['remaining']/60:.1f} min, "
          f"Restpegel {b['headroom']:.1f} dB(A), erschoepft={b['erschoepft']}")
    print(f"LCpeak max {state['lcpeak_max']:.1f} dB(C), LAF {state['laf']:.1f} dB(A)")
    print(f"Verlaufspunkte: {len(state['history'])}")

    # JSON-Tauglichkeit sicherstellen (kein NaN/Infinity)
    roh = json.dumps(state, allow_nan=False)
    print(f"JSON ok ({len(roh)} Zeichen)")

    # Weboberflaeche gegenpruefen
    app = sm.build_app(mon)
    client = app.test_client()
    r = client.get("/")
    print(f"GET /          -> {r.status_code}, {len(r.data)} Bytes")
    r = client.get("/api/state")
    print(f"GET /api/state -> {r.status_code}, Schluessel: {sorted(r.get_json())}")
    r = client.post("/api/settings", json={"laeq30": 96, "ziel": 92})
    print(f"POST /api/settings -> {r.status_code}, {r.get_json()}")
    r = client.get("/api/devices")
    print(f"GET /api/devices -> {r.status_code}, {len(r.get_json()['geraete'])} Geraete")
    r = client.post("/api/log", json={"aktiv": True})
    print(f"POST /api/log -> {r.status_code}, {r.get_json()}")
    client.post("/api/log", json={"aktiv": False})
    r = client.post("/api/reset-peaks")
    print(f"POST /api/reset-peaks -> {r.status_code}")


if __name__ == "__main__":
    main()
