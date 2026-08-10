"""
spl_core.py — Messkern für das Schallpegel-Monitoring.

Enthaelt die reine Signalverarbeitung, damit sie unabhaengig von Audiogeraet
und Weboberflaeche getestet werden kann:

  * A- und C-Bewertungsfilter nach IEC 61672-1
  * optionale Frequenzgang-Korrektur aus der Kalibrierdatei des Mikrofons
  * Zeitbewertung "Fast" (tau = 125 ms)
  * gleitende Mittelungspegel LAeq ueber frei waehlbare Fenster
  * getakteter 30-Minuten-Block (an volle/halbe Stunde ausgerichtet)
  * Restpegel-Prognose fuer den laufenden Block ("wieviel dB darf ich noch")
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
from scipy.signal import firwin2, lfilter, lfilter_zi, sosfilt, sosfilt_zi, sosfreqz, zpk2sos

# Eckfrequenzen der Bewertungskurven nach IEC 61672-1
_F1, _F2, _F3, _F4 = 20.598997, 107.65265, 737.86223, 12194.217
_TWO_PI = 2.0 * math.pi


def weighting_sos(kind: str, fs: float) -> np.ndarray:
    """
    Bewertungsfilter 'A' oder 'C' als SOS-Kaskade fuer die Abtastrate fs.

    Umsetzung ueber die Matched-Z-Transformation. Die sonst uebliche bilineare
    Transformation staucht bei 48 kHz die oberen Oktaven spuerbar (bei 12,5 kHz
    rund 2,6 dB zu wenig) — bei Musik mit viel Hochtonanteil wuerde das den
    Pegel zu niedrig anzeigen.
    """
    if kind == "A":
        zeros = [0.0, 0.0, 0.0, 0.0]
        poles = [
            -_TWO_PI * _F1, -_TWO_PI * _F1,
            -_TWO_PI * _F2,
            -_TWO_PI * _F3,
            -_TWO_PI * _F4, -_TWO_PI * _F4,
        ]
    elif kind == "C":
        zeros = [0.0, 0.0]
        poles = [
            -_TWO_PI * _F1, -_TWO_PI * _F1,
            -_TWO_PI * _F4, -_TWO_PI * _F4,
        ]
    else:
        raise ValueError("kind muss 'A' oder 'C' sein")

    zd = np.exp(np.asarray(zeros, dtype=float) / fs)
    pd = np.exp(np.asarray(poles, dtype=float) / fs)

    # Verstaerkung so normieren, dass die Kurve bei 1 kHz exakt 0 dB ergibt
    sos = zpk2sos(zd, pd, 1.0)
    _, h = sosfreqz(sos, worN=[_TWO_PI * 1000.0 / fs])
    return zpk2sos(zd, pd, 1.0 / abs(h[0]))


class Weighting:
    """Zustandsbehafteter Bewertungsfilter fuer fortlaufende Audioblöcke."""

    def __init__(self, kind: str, fs: float):
        self.sos = weighting_sos(kind, fs)
        self.zi = sosfilt_zi(self.sos) * 0.0

    def __call__(self, block: np.ndarray) -> np.ndarray:
        out, self.zi = sosfilt(self.sos, block, zi=self.zi)
        return out


class MicCorrection:
    """
    Gleicht den Eigen-Frequenzgang des Messmikrofons aus.

    Grundlage ist die Kalibrierdatei, wie sie z.B. miniDSP fuer jedes UMIK-1
    einzeln bereitstellt (Textdatei mit "Frequenz Pegel [Phase]" je Zeile).
    Die Kurve wird auf 1 kHz normiert und invertiert als linearphasiger
    FIR-Filter vor die Bewertung gehaengt — der Absolutpegel bleibt also der
    Kalibrierung ueberlassen, korrigiert wird nur der Verlauf.
    """

    def __init__(self, freqs, gains_db, fs: float, numtaps: int = 511):
        self.fs = fs
        self.numtaps = numtaps
        freqs = np.asarray(freqs, dtype=float)
        gains_db = np.asarray(gains_db, dtype=float)
        order = np.argsort(freqs)
        freqs, gains_db = freqs[order], gains_db[order]

        # auf 1 kHz normieren: dort soll die Korrektur genau 0 dB betragen
        gains_db = gains_db - np.interp(1000.0, freqs, gains_db)

        nyq = fs / 2.0
        inner = (freqs > 0) & (freqs < nyq)
        f = np.concatenate(([0.0], freqs[inner], [nyq]))
        g = np.concatenate(([gains_db[inner][0]], gains_db[inner], [gains_db[inner][-1]]))
        f, idx = np.unique(f, return_index=True)
        g = g[idx]

        amplitude = 10.0 ** (-g / 20.0)          # invertieren
        self.taps = firwin2(numtaps, f / nyq, amplitude)
        self.zi = lfilter_zi(self.taps, [1.0]) * 0.0
        self.delay = (numtaps - 1) // 2

    def __call__(self, block: np.ndarray) -> np.ndarray:
        out, self.zi = lfilter(self.taps, [1.0], block, zi=self.zi)
        return out

    @staticmethod
    def from_file(text: str, fs: float) -> tuple["MicCorrection", dict]:
        """
        Liest eine Kalibrierdatei (miniDSP-Format und aehnliche).
        Liefert den Filter und ein paar Eckdaten fuer die Anzeige.
        """
        freqs, gains, sens = [], [], None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith(("*", "#", '"')) or line[0].isalpha():
                low = line.lower().replace(" ", "")
                if "sensfactor=" in low:
                    try:
                        rest = low.split("sensfactor=", 1)[1]
                        sens = float(rest.replace("db", "").split(",")[0])
                    except ValueError:
                        pass
                continue
            parts = line.replace(",", " ").replace(";", " ").split()
            try:
                f, g = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                continue
            freqs.append(f)
            gains.append(g)

        if len(freqs) < 8:
            raise ValueError("Keine verwertbaren Messpunkte gefunden "
                             "(erwartet werden Zeilen aus Frequenz und Pegel).")

        info = {
            "punkte": len(freqs),
            "von_hz": round(min(freqs)),
            "bis_hz": round(max(freqs)),
            "sens_factor": sens,
        }
        return MicCorrection(freqs, gains, fs), info


class SlidingLeq:
    """Gleitender energetischer Mittelungspegel ueber ein Zeitfenster."""

    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self._buf: deque[tuple[float, float, float]] = deque()  # (t_ende, energie, dauer)
        self._sum = 0.0
        self._dur = 0.0

    def add(self, t_end: float, mean_square: float, duration: float) -> None:
        self._buf.append((t_end, mean_square * duration, duration))
        self._sum += mean_square * duration
        self._dur += duration
        cutoff = t_end - self.seconds
        while self._buf and self._buf[0][0] <= cutoff:
            _, energy, dur = self._buf.popleft()
            self._sum -= energy
            self._dur -= dur
        # Rundungsdrift abfangen
        if self._dur <= 0:
            self._sum, self._dur = 0.0, 0.0

    @property
    def filled(self) -> float:
        """Anteil des Fensters, der bereits mit Messwerten belegt ist (0..1)."""
        return min(1.0, self._dur / self.seconds) if self.seconds else 0.0

    def value(self, offset_db: float) -> float | None:
        if self._dur <= 0 or self._sum <= 0:
            return None
        return 10.0 * math.log10(self._sum / self._dur) + offset_db


@dataclass
class BlockState:
    """Ein an die Uhr ausgerichteter Messblock (Standard: 30 Minuten)."""

    length: float = 1800.0
    start: float = 0.0
    energy_sum: float = 0.0
    duration: float = 0.0
    peak_c: float = 0.0
    _last_boundary: float = field(default=0.0, repr=False)

    def boundary(self, now: float) -> float:
        return math.floor(now / self.length) * self.length

    def update(self, t_end: float, mean_square: float, duration: float, peak_c: float) -> bool:
        """Fuegt einen Block hinzu. Gibt True zurueck, wenn ein neuer Block begann."""
        rolled = False
        b = self.boundary(t_end)
        if b != self._last_boundary:
            self._last_boundary = b
            self.start = b
            self.energy_sum = 0.0
            self.duration = 0.0
            self.peak_c = 0.0
            rolled = True
        self.energy_sum += mean_square * duration
        self.duration += duration
        self.peak_c = max(self.peak_c, peak_c)
        return rolled

    def leq(self, offset_db: float) -> float | None:
        if self.duration <= 0 or self.energy_sum <= 0:
            return None
        return 10.0 * math.log10(self.energy_sum / self.duration) + offset_db

    def remaining(self, now: float) -> float:
        return max(0.0, self.start + self.length - now)

    def headroom(self, limit_db: float, offset_db: float, now: float) -> float | None:
        """
        Welchen Dauerpegel darf der Rest des Blocks noch haben, damit der
        Blockgrenzwert gerade eben eingehalten wird? (entspricht dem, was
        kommerzielle Systeme als MAM / Maximum Average Manager anzeigen)
        """
        rest = self.remaining(now)
        if rest <= 0:
            return None
        allowed_total = 10.0 ** ((limit_db - offset_db) / 10.0) * self.length
        left = allowed_total - self.energy_sum
        if left <= 0:
            return float("-inf")  # Grenzwert bereits ausgeschoepft
        return 10.0 * math.log10(left / rest) + offset_db


class ThirdOctave:
    """
    Terzband-Analyse (Real Time Analyzer).

    Arbeitet auf dem bereits A-bewerteten Signal — damit addieren sich die
    Bandpegel energetisch zum LAeq, und man sieht unmittelbar, welche Baender
    den Pegel treiben. Genau darum geht es bei der Pegelkontrolle: 6 dB zuviel
    im Bass kosten dasselbe Budget wie 6 dB zuviel ueberall.

    Die Bandmitten folgen IEC 61260 (Basis 10). Jedes Band bekommt das
    kuerzeste Analysefenster, das ihm noch genug Stuetzstellen laesst: hohe
    Baender sind breit und kommen mit 62 ms aus, das 20-Hz-Band (17,8 bis
    22,4 Hz) braucht eine ganze Sekunde. So reagiert die Anzeige oben herum
    flink, ohne dass die tiefen Baender ungenau werden.
    """

    G = 10.0 ** 0.3          # Frequenzverhaeltnis einer Oktave nach IEC 61260
    FENSTER = (0.0625, 0.125, 0.25, 0.5, 1.0)   # Kandidaten in Sekunden
    MIN_BINS = 4                                 # Stuetzstellen je Band

    def __init__(self, fs: float):
        self.fs = fs

        # Bandmitten von 20 Hz bis 20 kHz, sofern unterhalb der Nyquistgrenze
        self.centers: list[float] = []
        for n in range(-17, 14):
            fc = 1000.0 * self.G ** (n / 3.0)
            if fc * self.G ** (1 / 6.0) < fs / 2.0:
                self.centers.append(fc)

        # Fuer jedes Band das kuerzeste brauchbare Fenster waehlen
        self._fenster: dict[int, dict] = {}
        self.masken: list[tuple[int, np.ndarray]] = []
        for fc in self.centers:
            lo, hi = fc * self.G ** (-1 / 6.0), fc * self.G ** (1 / 6.0)
            breite = hi - lo
            laenge = next((l for l in self.FENSTER if breite * l >= self.MIN_BINS),
                          self.FENSTER[-1])
            n = int(fs * laenge)
            if n not in self._fenster:
                fenster = np.hanning(n)
                self._fenster[n] = {
                    "fenster": fenster,
                    "norm": n * float(np.sum(fenster ** 2)),
                    "freqs": np.fft.rfftfreq(n, 1.0 / fs),
                }
            freqs = self._fenster[n]["freqs"]
            maske = np.where((freqs >= lo) & (freqs < hi))[0]
            if maske.size == 0:   # sehr schmales Band: naechstgelegenen Bin nehmen
                maske = np.array([int(np.argmin(np.abs(freqs - fc)))])
            self.masken.append((n, maske))

        self.n_lang = max(self._fenster)          # laengster benoetigter Puffer

        self.power = np.zeros(len(self.centers))
        self.peak_db = np.full(len(self.centers), -99.0)
        self._letzte_zeit = None

    def fensterlaengen(self) -> dict[float, float]:
        """Welches Band mit welcher Fensterlaenge — fuer Selbsttest und Anzeige."""
        return {round(fc, 1): round(n / self.fs, 4)
                for fc, (n, _) in zip(self.centers, self.masken)}

    def _leistungsspektrum(self, signal: np.ndarray, n: int) -> np.ndarray:
        """Einseitiges Leistungsspektrum; die Summe ergibt den quadratischen Mittelwert."""
        eintrag = self._fenster[n]
        spektrum = np.fft.rfft(signal[-n:] * eintrag["fenster"])
        leistung = (np.abs(spektrum) ** 2) / eintrag["norm"]
        if leistung.size > 2:
            leistung[1:-1] *= 2.0
        return leistung

    def update(self, signal: np.ndarray, t: float, tau: float = 0.125) -> None:
        """
        signal ist der zuletzt aufgelaufene Puffer (A-bewertet), mindestens so
        lang wie das laengste Fenster. tau ist die Zeitkonstante der Glaettung.
        """
        if len(signal) < self.n_lang:
            return
        spektren = {n: self._leistungsspektrum(signal, n) for n in self._fenster}

        roh = np.empty(len(self.centers))
        for i, (n, maske) in enumerate(self.masken):
            roh[i] = float(np.sum(spektren[n][maske]))

        dt = 0.1 if self._letzte_zeit is None else max(1e-3, t - self._letzte_zeit)
        self._letzte_zeit = t
        alpha = math.exp(-dt / tau) if tau > 0 else 0.0
        self.power = self.power * alpha + roh * (1.0 - alpha)

        # Spitzenwerthaltung mit langsamem Abfall (10 dB je Sekunde)
        aktuell = 10.0 * np.log10(np.maximum(self.power, 1e-24))
        self.peak_db = np.maximum(aktuell, self.peak_db - 10.0 * dt)

    def snapshot(self, calibration_db: float) -> dict:
        gesamt = float(np.sum(self.power))
        if gesamt <= 0:
            return {"baender": [], "summe": None, "gruppen": {}}
        pegel = 10.0 * np.log10(np.maximum(self.power, 1e-24)) + calibration_db
        anteil = self.power / gesamt * 100.0

        baender = [
            {
                "f": round(fc, 1),
                "l": round(float(pegel[i]), 1),
                "peak": round(float(self.peak_db[i] + calibration_db), 1),
                "anteil": round(float(anteil[i]), 1),
            }
            for i, fc in enumerate(self.centers)
        ]

        def gruppe(lo, hi):
            m = [i for i, fc in enumerate(self.centers) if lo <= fc < hi]
            return round(float(np.sum(anteil[m])), 0) if m else 0.0

        return {
            "baender": baender,
            "summe": round(10.0 * math.log10(gesamt) + calibration_db, 1),
            "gruppen": {
                "tief": gruppe(0, 125),
                "mitten": gruppe(125, 2000),
                "hoehen": gruppe(2000, 1e9),
            },
        }


class SplEngine:
    """
    Nimmt fortlaufend Audioblöcke entgegen und haelt alle Anzeigewerte bereit.

    calibration_db ist der Offset, der aus dem digitalen Pegel (dBFS) einen
    Schalldruckpegel (dB SPL) macht: L = 20*log10(rms) + calibration_db
    """

    def __init__(
        self,
        fs: float = 48000.0,
        calibration_db: float = 120.0,
        windows: tuple[float, ...] = (60.0, 300.0, 1800.0, 3600.0),
        block_length: float = 1800.0,
    ):
        self.fs = fs
        self.calibration_db = calibration_db
        self.a = Weighting("A", fs)
        self.c = Weighting("C", fs)
        self.correction: MicCorrection | None = None
        self.correction_info: dict | None = None
        self.windows = {int(w): SlidingLeq(w) for w in windows}
        self.block = BlockState(length=block_length)
        self.spektrum = ThirdOctave(fs)
        self.spektrum_tau = 0.125     # Zeitbewertung der Anzeige (Fast)
        self._spek_blocks: deque[np.ndarray] = deque()
        self._spek_laenge = 0

        self._fast_alpha = None  # wird beim ersten Block gesetzt
        self._fast_state = 0.0
        self.laf = None
        self.laf_max = 0.0
        self.lcpeak_max = 0.0
        self.history: deque[tuple[float, float]] = deque(maxlen=3600)  # (t, LAeq 1s)
        self.started = None
        # Intern laeuft alles ueber eine monotone Uhr: springt die Systemzeit
        # (etwa weil der Rechner sie nachtraeglich stellt), bleiben gleitende
        # Fenster und Mittelungen unversehrt. Die Wanduhr kommt nur fuer
        # Anzeige, Protokoll und die halbstuendigen Bloecke ins Spiel.
        self.uhr_offset = time.time() - time.monotonic()
        self.clipping = False
        self._clip_until = 0.0

    # -- Hilfsfunktionen ---------------------------------------------------
    def _db(self, amplitude: float) -> float:
        return 20.0 * math.log10(max(amplitude, 1e-12)) + self.calibration_db

    def set_calibration(self, calibration_db: float) -> None:
        self.calibration_db = float(calibration_db)

    def calibration_from_reference(self, rms: float, reference_db: float = 94.0) -> float:
        """Offset berechnen, waehrend der Kalibrator (z.B. 94 dB) aufgesteckt ist."""
        return reference_db - 20.0 * math.log10(max(rms, 1e-12))

    # -- Hauptschleife -----------------------------------------------------
    def process(self, samples: np.ndarray, t_end: float | None = None) -> dict:
        if t_end is None:
            t_end = time.monotonic()
        if self.started is None:
            self.started = t_end

        samples = np.asarray(samples, dtype=np.float64).reshape(-1)
        duration = len(samples) / self.fs
        rms_raw = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
        peak_raw = float(np.max(np.abs(samples))) if len(samples) else 0.0

        if self.correction is not None:
            samples = self.correction(samples)

        a_sig = self.a(samples)
        c_sig = self.c(samples)

        mean_square = float(np.mean(a_sig ** 2))
        peak_c_lin = float(np.max(np.abs(c_sig))) if len(c_sig) else 0.0

        # Uebersteuerung des Wandlers melden (bleibt 3 s stehen)
        if peak_raw >= 0.999:
            self._clip_until = t_end + 3.0
        self.clipping = t_end < self._clip_until

        # Zeitbewertung Fast: einpoliger Tiefpass auf dem quadrierten Signal
        if self._fast_alpha is None:
            self._fast_alpha = math.exp(-1.0 / (self.fs * 0.125))
        alpha = self._fast_alpha
        sq = a_sig ** 2
        # blockweise exakt genug: Tiefpass ueber den Blockmittelwert nachfuehren
        n = len(sq)
        decay = alpha ** n
        self._fast_state = self._fast_state * decay + float(np.mean(sq)) * (1.0 - decay)
        self.laf = 10.0 * math.log10(max(self._fast_state, 1e-24)) + self.calibration_db

        for w in self.windows.values():
            w.add(t_end, mean_square, duration)
        rolled = self.block.update(t_end + self.uhr_offset, mean_square,
                                   duration, peak_c_lin)

        lcpeak = self._db(peak_c_lin)
        self.lcpeak_max = max(self.lcpeak_max, lcpeak)
        self.laf_max = max(self.laf_max, self.laf)

        # Puffer fuer die Terzanalyse: gerade so lang wie das lange Fenster
        self._spek_blocks.append(a_sig)
        self._spek_laenge += len(a_sig)
        while self._spek_laenge - len(self._spek_blocks[0]) >= self.spektrum.n_lang:
            self._spek_laenge -= len(self._spek_blocks.popleft())
        if self._spek_laenge >= self.spektrum.n_lang:
            self.spektrum.update(np.concatenate(self._spek_blocks), t_end,
                                 tau=self.spektrum_tau)

        laeq_short = 10.0 * math.log10(max(mean_square, 1e-24)) + self.calibration_db
        if not self.history or t_end - self.history[-1][0] >= 1.0:
            self.history.append((t_end, laeq_short))

        return {
            "t": t_end,
            "laeq_block": laeq_short,
            "lcpeak": lcpeak,
            "block_rolled": rolled,
            "rms_raw": rms_raw,
        }

    def stelle_uhr(self, wanduhr: float | None = None) -> None:
        """
        Wanduhr neu einlesen (nachdem die Systemzeit gestellt wurde) und den
        laufenden Halbstundenblock verwerfen — er laege sonst falsch.
        """
        jetzt = time.monotonic()
        self.uhr_offset = (time.time() if wanduhr is None else wanduhr) - jetzt
        self.block = BlockState(length=self.block.length)

    def wanduhr(self, t: float | None = None) -> float:
        """Rechnet eine interne Zeitmarke in Systemzeit um."""
        return (time.monotonic() if t is None else t) + self.uhr_offset

    def snapshot(self, limits: dict) -> dict:
        now = time.monotonic() + self.uhr_offset
        out = {
            "laf": self.laf,
            "laf_max": self.laf_max if self.laf is not None else None,
            "lcpeak_max": self.lcpeak_max if self.laf is not None else None,
            "clipping": self.clipping,
            "calibration_db": self.calibration_db,
            "running_since": self.wanduhr(self.started) if self.started else None,
            "windows": {},
        }
        for sec, w in self.windows.items():
            out["windows"][str(sec)] = {
                "value": w.value(self.calibration_db),
                "filled": w.filled,
            }
        block_leq = self.block.leq(self.calibration_db)
        head = self.block.headroom(limits["laeq30"], self.calibration_db, now)
        erschoepft = head is not None and math.isinf(head)
        out["block"] = {
            "erschoepft": erschoepft,
            "start": self.block.start,
            "length": self.block.length,
            "remaining": self.block.remaining(now),
            "leq": block_leq,
            "lcpeak": self._db(self.block.peak_c) if self.block.peak_c > 0 else None,
            "headroom": None if erschoepft else head,
        }
        out["spektrum"] = self.spektrum.snapshot(self.calibration_db)
        return out
