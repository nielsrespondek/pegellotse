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

"""Selbsttest: prueft Bewertungsfilter und Pegelrechnung gegen bekannte Sollwerte."""

import math

import numpy as np
from scipy.signal import sosfreqz

from messkern import SplEngine, ThirdOctave, weighting_sos

FS = 48000.0
# Sollwerte der A-Bewertung nach IEC 61672-1, Tabelle 2 (dB)
A_SOLL = {31.5: -39.4, 63: -26.2, 125: -16.1, 250: -8.6, 500: -3.2,
          1000: 0.0, 2000: 1.2, 4000: 1.0, 8000: -1.1, 12500: -4.3}
C_SOLL = {31.5: -3.0, 63: -0.8, 125: -0.2, 250: 0.0, 500: 0.0,
          1000: 0.0, 2000: -0.2, 4000: -0.8, 8000: -3.0, 12500: -6.2}
# Toleranzgrenzen Klasse 1 nach IEC 61672-1 (obere/untere Abweichung in dB)
TOLERANZ = {31.5: (1.5, 2.0), 63: (1.0, 1.5), 125: (1.0, 1.0), 250: (1.0, 1.0),
            500: (1.0, 1.0), 1000: (0.7, 0.7), 2000: (1.0, 1.0), 4000: (1.0, 1.0),
            8000: (1.5, 2.5), 12500: (3.0, 6.0)}


def response(kind, freqs_hz):
    sos = weighting_sos(kind, FS)
    w = 2 * np.pi * np.array(freqs_hz) / FS
    _, h = sosfreqz(sos, worN=w)
    return 20 * np.log10(np.abs(h))


def check_curves():
    ok = True
    for kind, soll in (("A", A_SOLL), ("C", C_SOLL)):
        got = response(kind, list(soll.keys()))
        print(f"\n{kind}-Bewertung:")
        for (f, s), g in zip(soll.items(), got):
            diff = g - s
            oben, unten = TOLERANZ[f]
            passt = -unten <= diff <= oben
            ok = ok and passt
            flag = "innerhalb Klasse 1" if passt else "AUSSERHALB Klasse 1"
            print(f"  {f:>6} Hz   soll {s:>6.1f}   ist {g:>6.1f}   "
                  f"({diff:+.2f} dB, {flag})")
    return ok


def check_levels():
    """1 kHz Sinus mit bekanntem Pegel muss den erwarteten LAeq ergeben."""
    print("\nPegelrechnung:")
    eng = SplEngine(fs=FS, calibration_db=94.0)  # 0 dBFS RMS entspricht 94 dB
    t = np.arange(int(FS * 10)) / FS
    amp = 10 ** (-20 / 20) * math.sqrt(2)  # RMS = -20 dBFS  ->  74 dB SPL
    sig = amp * np.sin(2 * np.pi * 1000 * t)
    t0 = 0.0
    for i in range(0, len(sig), 4800):
        t0 += 0.1
        eng.process(sig[i:i + 4800], t_end=t0)
    laeq60 = eng.windows[60].value(eng.calibration_db)
    print(f"  LAeq (1 min) soll 74.0   ist {laeq60:.2f}")
    ok = abs(laeq60 - 74.0) < 0.2

    # Halbierte Dauer bei gleicher Energie -> +3 dB Merkregel pruefen
    eng2 = SplEngine(fs=FS, calibration_db=94.0)
    t2 = 0.0
    for i in range(0, len(sig) // 2, 4800):
        t2 += 0.1
        eng2.process(sig[i:i + 4800], t_end=t2)
    for _ in range(50):  # 5 s Stille
        t2 += 0.1
        eng2.process(np.zeros(4800), t_end=t2)
    laeq_mix = eng2.windows[60].value(eng2.calibration_db)
    print(f"  5 s Signal + 5 s Stille soll 71.0   ist {laeq_mix:.2f}")
    ok = ok and abs(laeq_mix - 71.0) < 0.2
    return ok


def check_headroom():
    """Restpegel-Prognose: nach halbem Block auf Grenzwert -> Rest darf gleich viel."""
    print("\nRestpegel-Prognose:")
    from messkern import BlockState
    b = BlockState(length=1800.0)
    b.start = 0.0
    b._last_boundary = 0.0
    b.energy_sum = 10 ** (99 / 10) * 900  # 900 s exakt auf 99 dB
    b.duration = 900.0
    head = b.headroom(99.0, 0.0, now=900.0)
    print(f"  halber Block auf 99 dB  ->  Rest darf {head:.1f} dB (soll 99.0)")
    ok = abs(head - 99.0) < 0.05

    b2 = BlockState(length=1800.0)
    b2.start, b2._last_boundary = 0.0, 0.0
    b2.energy_sum = 10 ** (96 / 10) * 900  # 3 dB unter dem Grenzwert
    b2.duration = 900.0
    head2 = b2.headroom(99.0, 0.0, now=900.0)
    print(f"  halber Block auf 96 dB  ->  Rest darf {head2:.1f} dB (soll 100.8)")
    ok = ok and abs(head2 - 100.76) < 0.1
    return ok


def check_spektrum():
    """Terzanalyse: Bandsumme muss dem Gesamtpegel entsprechen, Toene im richtigen Band."""
    print("\nTerzanalyse:")

    def speise(engine, fn, sekunden=3.0):
        n = int(FS * 0.1)
        t = 0.0
        for i in range(int(sekunden * 10)):
            zeit = (np.arange(n) + i * n) / FS
            t += 0.1
            engine.process(fn(zeit), t_end=t)
        return engine

    to = ThirdOctave(FS)
    print(f"  {len(to.centers)} Baender von {to.centers[0]:.0f} bis {to.centers[-1]:.0f} Hz")
    ok = len(to.centers) == 31

    # Rauschen: Summe ueber alle Baender = LAeq
    rng = np.random.default_rng(3)
    eng = SplEngine(fs=FS, calibration_db=94.0, windows=(10.0,))
    speise(eng, lambda t: rng.normal(0, 0.05, len(t)))
    snap = eng.snapshot({"laeq30": 99.0})
    summe = snap["spektrum"]["summe"]
    laeq = snap["windows"]["10"]["value"]
    print(f"  Rauschen: Bandsumme {summe:.2f} dB(A), LAeq 10 s {laeq:.2f} dB(A) "
          f"(Differenz {summe - laeq:+.2f} dB)")
    ok = ok and abs(summe - laeq) < 0.6

    # Einzeltoene muessen im zugehoerigen Band landen
    amp = 10 ** (-20 / 20) * math.sqrt(2)
    for frequenz, erwartet in ((50, 50.1), (1000, 1000.0), (6300, 6309.6)):
        e = SplEngine(fs=FS, calibration_db=94.0, windows=(10.0,))
        speise(e, lambda t, f=frequenz: amp * np.sin(2 * np.pi * f * t))
        baender = e.snapshot({"laeq30": 99.0})["spektrum"]["baender"]
        lautestes = max(baender, key=lambda b: b["l"])
        treffer = abs(lautestes["f"] - erwartet) < 1.0 and lautestes["anteil"] > 95
        ok = ok and treffer
        print(f"  Sinus {frequenz:>5} Hz -> Band {lautestes['f']:.1f} Hz, "
              f"{lautestes['anteil']:.0f} % des Pegels "
              f"({'ok' if treffer else 'FALSCHES BAND'})")
    return ok


if __name__ == "__main__":
    results = [check_curves(), check_levels(), check_headroom(), check_spektrum()]
    print("\n" + ("Alle Tests bestanden." if all(results) else "FEHLER: siehe oben."))
