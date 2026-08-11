# Pegellotse

**Schallpegel-Monitoring für Veranstaltungen — offline, im Browser, auf Windows oder Raspberry Pi.**

Ein Lotse fährt das Schiff nicht, er sagt, wo es langgeht. Genau das macht
dieses Programm mit dem Schallpegel: es zeigt nicht nur, wie laut es gerade
ist, sondern welchen Pegel der Rest der laufenden halben Stunde noch haben
darf, damit der Grenzwert am Ende eingehalten ist.

Entstanden für Gemeindefeste, Konzerte und Jugendveranstaltungen, bei denen
niemand ein Messsystem für mehrere tausend Euro anschafft, aber trotzdem
jemand ein Auge auf die Ohren des Publikums haben sollte.

---

## Was es anzeigt

- **Momentanpegel** mit Zeitbewertung Fast und einem Balken gegen den selbst
  gesetzten Zielpegel — zum Aussteuern am Pult
- **LAeq** gleitend über 10 Sekunden, 1, 5, 30 und 60 Minuten
- **Restpegel** für den laufenden, an die Uhr gebundenen 30-Minuten-Block:
  welchen Dauerpegel der Rest noch haben darf
- **LCpeak** und LAF als Spitzenwerte
- **Terzspektrum** in Echtzeit, A-bewertet, mit Klartextangabe, welche Bänder
  den Pegel gerade treiben
- **Protokoll** als CSV, eine Zeile je Sekunde, im Browser herunterladbar, mit
  Markierungen für wichtige Momente

Die Anzeige läuft im Browser und ist im selben Netz auch vom Tablet oder Handy
erreichbar — auf dem Raspberry Pi unter `http://pegellotse.local`, unter
Windows unter `http://localhost:8000`. Es geht nichts ins Internet.

---

## Installation

### Windows

Unter [Releases](../../releases) das ZIP `Pegellotse-portabel.zip`
herunterladen, **vollständig entpacken** und `Pegellotse starten.bat`
doppelklicken.

Python muss nicht installiert sein, es liegt im Paket bei. Direkt aus dem ZIP
heraus startet es nicht — der Ordner muss wirklich entpackt sein.

Bewusst keine EXE: eine unsignierte Programmdatei wird von Smart App Control
blockiert, und daran ändert keine Verpackung etwas. Das eingebettete Python
stammt von python.org und ist signiert, das Paket läuft deshalb überall.

### Raspberry Pi

Sinnvoll, wenn das Mikrofon dort stehen soll, wo nach DIN gemessen wird — am
lautesten öffentlich zugänglichen Platz — und nicht am Pult. USB reicht nur
etwa fünf Meter weit, über WLAN ist die Entfernung egal.

Empfohlen: Raspberry Pi 4 (ein 3 B genügt rechnerisch, funkt aber nur auf
2,4 GHz, und genau das Band ist bei Veranstaltungen voll), Netzteil mit
5,1 V / 3 A, Speicherkarte ab 16 GB.

**1. System schreiben.** Raspberry Pi OS Lite (64-bit) mit dem Raspberry Pi
Imager auf die Karte schreiben. Im Imager unter dem Zahnrad vorher eintragen:

- Hostname, zum Beispiel `pegellotse`
- Benutzername und Kennwort
- WLAN samt Ländercode
- SSH aktivieren
- Zeitzone

Damit braucht der Pi nie Bildschirm oder Tastatur.

**2. Installieren.** Karte einlegen, Pi starten, dann von einem anderen
Rechner aus:

```bash
ssh benutzer@pegellotse.local
curl -fsSL https://raw.githubusercontent.com/nielsrespondek/pegellotse/main/install.sh | sudo bash
```

Das Skript installiert die Pakete, richtet eine Python-Umgebung ein, prüft die
Messkette mit dem Selbsttest, legt einen eigenen Dienstbenutzer an und startet
den Dienst. Ab dem nächsten Einschalten läuft er von allein. Am Ende nennt es
die Adressen, unter denen das Dashboard erreichbar ist.

Einstellbar über Umgebungsvariablen:

| Variable | Bedeutung |
|---|---|
| `REPO` | andere Quelle auf GitHub, etwa ein Fork |
| `PORT` | Port des Dashboards, Standard 80 |
| `HOSTNAME_NEU` | Rechnernamen setzen |
| `HOTSPOT=0` | Notfall-Zugangspunkt nicht einrichten |
| `HOTSPOT_SSID`, `HOTSPOT_PW` | Name und Kennwort des Zugangspunkts |
| `LUEFTER_GPIO`, `LUEFTER_TEMP` | temperaturgesteuerter Lüfter |

**3. Aufrufen.** `http://pegellotse.local`

Der Dienst hört auf Port 80, die Adresse braucht also keine Portangabe. Dafür
bekommt er von systemd genau eine zusätzliche Fähigkeit
(`CAP_NET_BIND_SERVICE`) — Ports unter 1024 darf sonst nur root öffnen. Der
Dienst selbst läuft weiterhin als eigener, unprivilegierter Benutzer. Mit
`PORT=8000` beim Installieren lässt sich ein anderer Port wählen.

Das klappt von iPhone, iPad, Mac und Windows zuverlässig. Unter Android ist
die Auflösung von `.local`-Namen wackelig — dort hilft eine im Router fest
vergebene Adresse.

```bash
systemctl status pegellotse      # Zustand ansehen
journalctl -u pegellotse -f      # Meldungen mitlesen
```

Konfiguration und Protokolle liegen in `/var/lib/pegellotse`.

---

## Erste Schritte

**Mikrofon wählen.** Einstellungen → Mikrofon listet alle Aufnahmegeräte auf.
Wird ein Mikrofon erst nach dem Start eingesteckt, taucht es dort erst nach
einem Druck auf *Geräte neu suchen* auf.
Unter Windows vorher alle Mikrofoneffekte abschalten, Format auf 48000 Hz
stellen und den Aufnahmepegel auf einen festen Wert setzen — jede spätere
Änderung macht die Kalibrierung ungültig.

**Kalibrieren.** Mit Kalibrator: Referenzpegel eintragen (üblich 94 dB) und
*Einmessen* drücken. Ohne Kalibrator: den Offset von Hand eintragen, etwa aus
einer bereits eingemessenen Software.

Ohne Kalibrierung zeigt jedes Mikrofon irgendetwas an. Die Relativwerte
stimmen dann zwar — 10 dB lauter bleibt 10 dB lauter — der Absolutwert aber
nicht.

**Frequenzgang.** Einstellungen → Frequenzgang nimmt die individuelle
Kalibrierdatei des Mikrofons entgegen, etwa die Datei, die miniDSP zur
Seriennummer eines UMIK-1 bereitstellt. Erwartet werden Textzeilen aus
Frequenz und Pegel. Die Kurve wird auf 1 kHz normiert und invertiert als
linearphasiger FIR-Filter vorgeschaltet: korrigiert wird nur der Verlauf, den
Absolutpegel bestimmt weiterhin die Kalibrierung.

**Protokolle.** Der Knopf unten schaltet die Aufzeichnung an und aus; je
Aufzeichnung entsteht eine CSV-Datei, benannt nach Zeitpunkt und
Veranstaltung. Unter Einstellungen → Protokolle lassen sich die Dateien
einzeln herunterladen, alle zusammen als ZIP holen und einzeln löschen — auf
dem Raspberry Pi also ohne SSH und ohne Speicherkarte auszubauen. Die gerade
laufende Aufzeichnung lässt sich nicht löschen.

Getrennt wird mit Semikolon, das Dezimalzeichen ist ein Komma; Excel und
LibreOffice öffnen die Dateien direkt.

**Markierungen.** Das Feld unten schreibt eine Bemerkung in die nächste
Protokollzeile — Bandwechsel, Soundcheck beendet, Beschwerde eines Anwohners.
Beim späteren Nachsehen findet man damit die Stelle wieder, um die es geht.

**Grenzwerte.** Voreingestellt sind die Werte aus DIN 15905-5. Der Zielpegel
ist frei wählbar und bestimmt, ab wann die Anzeige gelb wird.

## Richtwerte nach DIN 15905-5

Dieselbe Übersicht steht im Dashboard unter Einstellungen.

| Wert | Bedeutung |
|---|---|
| **85 dB(A)** | Wird dieser Beurteilungspegel erwartet, ist das Publikum auf die mögliche Gehörgefährdung hinzuweisen — Durchsage oder Aushang. |
| **95 dB(A)** | Ab hier ist kostenloser Gehörschutz anzubieten und zum Tragen aufzufordern. Wird keiner angeboten, ist bei 95 dB(A) auch die Obergrenze erreicht. |
| **99 dB(A)** | Höchster Beurteilungspegel, gemittelt über jede volle halbe Stunde. Darüber ist Schluss, auch mit Gehörschutz. |
| **135 dB(C)** | Spitzenschalldruck, der zu keinem Zeitpunkt überschritten werden darf. |
| **Messort** | Der lauteste dem Publikum zugängliche Platz. Steht das Mikrofon woanders, gehört ein Korrekturwert dazu. |
| **Gerät** | Ein Schallpegelmesser der Klasse 2 genügt; er ist vor und nach der Messung zu kalibrieren. Geeicht muss er nicht sein. |
| **Dauer** | Gemessen wird über die gesamte Betriebsdauer der Beschallungsanlage, beendet erst nach dem Abschalten. |

Quelle: DIN 15905-5:2022-07 „Veranstaltungstechnik – Tontechnik – Teil 5:
Maßnahmen zum Vermeiden einer Gehörgefährdung des Publikums durch hohe
Schallemissionen elektroakustischer Beschallungstechnik“.

Die Norm selbst ist kein Gesetz; verbindlich wird sie über behördliche
Auflagen oder Genehmigungen. Unabhängig davon gilt die Verkehrssicherungs-
pflicht des Veranstalters, deren Umfang der Bundesgerichtshof 2001
(VI ZR 142/00) an dieser Norm bemessen hat — ein Messprotokoll ist im
Streitfall der Nachweis, dass die Pegel eingehalten wurden. Für Beschäftigte
gilt zusätzlich die Lärm- und Vibrations-Arbeitsschutzverordnung.

Das hier ist eine Gedächtnisstütze, keine Rechtsberatung.

---

## Im Dauerbetrieb

Ein Wächter prüft alle fünf Sekunden, ob noch Ton ankommt. Bleibt er aus —
Kabel raus, Mikrofon abgemeldet, Treiber weg — startet er die Aufnahme von
selbst neu und schreibt einen Hinweis ins Dashboard. Ohne das bliebe ein
herausgerutschtes USB-Kabel unbemerkt, bis jemand zufällig hinschaut.

Kommt über eine Minute lang gar kein Signal an, erscheint zusätzlich eine
Warnung. Das ist das Muster eines toten Mikrofons, und in den Zahlen sieht es
sonst aus wie eine sehr leise Veranstaltung.

Solange das Dashboard geöffnet ist, hält es den Bildschirm wach. Das
funktioniert allerdings nur bei gesicherter Verbindung, in der Praxis also auf
dem Rechner selbst — auf dem Tablet über `http://` bleibt es beim
Bildschirmzeitlimit des Geräts.

## Netz und Uhrzeit auf dem Pi

**WLAN nachtragen.** Unter Einstellungen → WLAN lassen sich Netze suchen und
hinzufügen. Neue Netze werden standardmäßig nur gespeichert, nicht sofort
aktiviert — sonst wechselt der Pi mitten im Betrieb das Netz und reißt die
gerade offene Verbindung ab.

**Wenn kein Netz da ist**, macht der Pi nach etwa anderthalb Minuten selbst
ein WLAN auf (Standard: `Pegellotse` / `pegellotse`, Adresse
`http://10.42.0.1`). Darüber lässt sich in Ruhe das richtige Netz
eintragen. Zurück geht es nicht von allein: die WLAN-Chips im Pi können nicht
gleichzeitig senden und nach fremden Netzen suchen. Also Zugangspunkt im
Dashboard beenden oder neu starten.

**Uhrzeit.** Ein Pi hat keine batteriegepufferte Uhr. Das Dashboard vergleicht
beim Öffnen die Uhr des Pi mit der des Endgeräts, das gerade daraufschaut, und
bietet an, sie zu übernehmen. Während einer laufenden Aufzeichnung wird die
Uhr nicht gestellt.

Den Messwerten kann eine springende Uhr nichts anhaben: die gleitenden
Mittelungen laufen über eine monotone Uhr, die Systemzeit dient nur für
Anzeige, Protokoll und die Lage der halbstündigen Blöcke.

---

## Genauigkeit

Der Rechenweg ist im Selbsttest belegt (`python selftest.py`):

- A- und C-Bewertung liegen an allen Stützstellen von 31,5 Hz bis 12,5 kHz
  innerhalb der Toleranz für **Klasse 1 nach IEC 61672-1**, im mittleren
  Bereich unter 0,15 dB
- die energetische Mittelung stimmt auf 0,01 dB gegen von Hand nachgerechnete
  Fälle
- die Terzbänder nach IEC 61260 addieren sich auf 0,1 dB genau zum
  Gesamtpegel

Die Bewertungsfilter sind über die Matched-Z-Transformation umgesetzt. Die
sonst übliche bilineare Transformation staucht bei 48 kHz die oberen Oktaven
spürbar — bei 12,5 kHz rund 2,6 dB — und würde bei hochtonlastiger Musik zu
niedrig anzeigen.

**Der Absolutpegel hängt allein an der Kalibrierung.** Ist der Offset falsch,
ist jede Anzeige um denselben Betrag falsch, so sauber die Rechnung sonst auch
ist.

## Grenzen

Ein Messwerkzeug zur eigenen Orientierung, **kein normkonformes
Schallpegelmessgerät**. Für einen Nachweis gegenüber einer Behörde fehlen die
Bauartzulassung des Gesamtsystems nach DIN EN 61672-1, gültige
Kalibrierscheine für Mikrofon und Kalibrator sowie eine manipulationssichere
Protokollierung.

Fachlich sauber sind dagegen Bewertungskurven, Zeitbewertung, energetische
Mittelung und der an die Uhr gebundene 30-Minuten-Block. Es fehlt die
Beweiskraft, nicht die Physik.

Gemessen wird nach DIN 15905-5 am lautesten Platz, der dem Publikum
zugänglich ist. Steht das Mikrofon woanders, gehört die Pegeldifferenz dazu.

---

## Selbst bauen

```bash
pip install -r requirements.txt
python pegellotse.py          # startet direkt
python selftest.py            # prüft die Messkette
python testlauf.py            # Probelauf ohne Mikrofon
python paket_bauen.py --zip   # baut die portable Windows-Version
```

`paket_bauen.py` lädt das eingebettete Python von python.org, richtet pip ein,
installiert die Abhängigkeiten und prüft das fertige Paket, indem es den
Selbsttest damit laufen lässt.

Auf GitHub baut `.github/workflows/build.yml` dasselbe Paket bei jedem
Versions-Tag und hängt es an ein Release:

```bash
git tag v1.0.1
git push origin v1.0.1
```

### Aufbau

| Datei | Inhalt |
|---|---|
| `pegellotse.py` | Audio, Protokoll, Webserver, Einstellungen |
| `messkern.py` | Filter, Mittelungen, Terzanalyse |
| `templates/dashboard.html` | die Oberfläche |
| `selftest.py` | prüft die Messkette gegen Sollwerte |
| `paket_bauen.py` | baut die portable Windows-Version |
| `install.sh`, `hotspot.sh` | Einrichtung auf dem Raspberry Pi |

Windows und Raspberry Pi teilen sich denselben Quelltext; getrennt sind nur
die Dateien, die verpacken oder einrichten.

---

## Lizenz

Freie Software unter der **GNU General Public License, Version 3** — der
vollständige Text steht in [LICENSE](LICENSE).

Benutzen, weitergeben und verändern ist ausdrücklich erwünscht. Wer eine
veränderte Fassung weitergibt, gibt den Quelltext dazu ebenfalls unter der GPL
heraus.

Ohne jede Gewährleistung. Wer damit misst, misst auf eigene Verantwortung.
