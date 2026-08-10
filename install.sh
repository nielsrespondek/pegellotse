#!/usr/bin/env bash
#
# Pegellotse — Schallpegel-Monitoring fuer Veranstaltungen
# Copyright (C) 2026 nielsrespondek
#
# Dieses Programm ist freie Software: Sie koennen es weitergeben und/oder
# veraendern unter den Bedingungen der GNU General Public License, Version 3,
# wie von der Free Software Foundation veroeffentlicht.
#
# Die Veroeffentlichung erfolgt in der Hoffnung, dass es nuetzlich ist, aber
# OHNE JEDE GEWAEHRLEISTUNG — sogar ohne die implizite Gewaehrleistung der
# MARKTFAEHIGKEIT oder EIGNUNG FUER EINEN BESTIMMTEN ZWECK. Einzelheiten in
# der GNU General Public License, mitgeliefert als Datei LICENSE.
#
# install.sh — richtet das Pegellotse auf einem Raspberry Pi ein.
#
# Aus dem Netz:
#     curl -fsSL https://raw.githubusercontent.com/DEINNAME/pegellotse/main/install.sh | sudo bash
#
# Aus einem ausgecheckten Ordner (funktioniert auch ohne GitHub):
#     sudo ./install.sh
#
# Einstellbar ueber Umgebungsvariablen:
#     REPO=benutzer/projekt   Quelle auf GitHub
#     BRANCH=main             Zweig
#     PORT=8000               Port des Dashboards
#     HOSTNAME_NEU=pegellotse Rechnernamen setzen (Aufruf ueber name.local)
#     LUEFTER_GPIO=12         temperaturgesteuerter Luefter an diesem GPIO
#     LUEFTER_TEMP=55         Einschalttemperatur in Grad (Standard 55)
#     HOTSPOT=0               Notfall-Zugangspunkt nicht einrichten
#     HOTSPOT_SSID=Pegellotse         Name des Zugangspunkts
#     HOTSPOT_PW=pegellotse           Kennwort (mindestens 8 Zeichen)
#
set -euo pipefail

REPO="${REPO:-DEINNAME/pegellotse}"
BRANCH="${BRANCH:-main}"
PORT="${PORT:-8000}"
ZIEL="${ZIEL:-/opt/pegellotse}"
DATEN="${DATEN:-/var/lib/pegellotse}"
DIENST="pegellotse"
BENUTZER="pegellotse"

sage() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fehler() { printf '\n\033[31mAbgebrochen: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fehler "Bitte mit sudo starten."
command -v apt-get >/dev/null || fehler "Erwartet wird Raspberry Pi OS oder Debian."

sage "Pakete installieren"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# numpy und scipy kommen aus den Paketquellen — auf dem Pi spart das ein
# langes Uebersetzen. sounddevice holt sich pip, es ist winzig.
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    python3-numpy python3-scipy python3-flask \
    libportaudio2 avahi-daemon curl ca-certificates

sage "Programmdateien ablegen"
mkdir -p "$ZIEL"
QUELLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$QUELLE/pegellotse.py" ]; then
    echo "  aus dem oertlichen Ordner: $QUELLE"
    cp -r "$QUELLE"/pegellotse.py "$QUELLE"/messkern.py "$QUELLE"/selftest.py \
          "$QUELLE"/requirements.txt "$QUELLE"/LICENSE "$ZIEL"/
    [ -f "$QUELLE/hotspot.sh" ] && cp "$QUELLE"/hotspot.sh "$ZIEL"/ || true
    cp -r "$QUELLE"/templates "$ZIEL"/
    [ -d "$QUELLE/beispiel" ] && cp -r "$QUELLE"/beispiel "$ZIEL"/ || true
else
    [ "$REPO" = "DEINNAME/pegellotse" ] && fehler \
        "Keine Programmdateien gefunden und REPO ist nicht gesetzt.
   Entweder das Skript im Projektordner starten oder REPO angeben:
   curl -fsSL .../install.sh | sudo REPO=benutzer/projekt bash"
    echo "  von GitHub: $REPO ($BRANCH)"
    TMP="$(mktemp -d)"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" \
        | tar xz -C "$TMP" --strip-components=1 \
        || fehler "Herunterladen fehlgeschlagen. Stimmen REPO und BRANCH?"
    cp -r "$TMP"/pegellotse.py "$TMP"/messkern.py "$TMP"/selftest.py \
          "$TMP"/requirements.txt "$TMP"/LICENSE "$TMP"/templates "$ZIEL"/
    [ -f "$TMP/hotspot.sh" ] && cp "$TMP"/hotspot.sh "$ZIEL"/ || true
    [ -d "$TMP/beispiel" ] && cp -r "$TMP"/beispiel "$ZIEL"/ || true
    rm -rf "$TMP"
fi

# Dateien, die ueber Windows gewandert sind, tragen manchmal CRLF-Zeilenenden.
# Python stoert das nicht, Shell-Skripte schon — also vorsorglich glaetten.
sed -i 's/\r$//' "$ZIEL"/*.py "$ZIEL"/*.sh 2>/dev/null || true

sage "Python-Umgebung einrichten"
# --system-site-packages: numpy, scipy und flask kommen aus den Paketquellen
python3 -m venv --system-site-packages "$ZIEL/venv"
"$ZIEL/venv/bin/pip" install --quiet --upgrade pip
"$ZIEL/venv/bin/pip" install --quiet sounddevice
"$ZIEL/venv/bin/python" -c "import numpy, scipy, flask, sounddevice" \
    || fehler "Eine Abhaengigkeit fehlt."

sage "Messkette pruefen"
( cd "$ZIEL" && "$ZIEL/venv/bin/python" selftest.py ) || fehler "Der Selbsttest ist durchgefallen."

sage "Dienstbenutzer und Verzeichnisse"
id -u "$BENUTZER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$DATEN" --shell /usr/sbin/nologin "$BENUTZER"
usermod -aG audio "$BENUTZER"
mkdir -p "$DATEN"
chown -R "$BENUTZER:$BENUTZER" "$DATEN"

# Der Dienst darf die Uhr stellen und WLAN-Zugaenge verwalten — sonst nichts.
cat > /etc/sudoers.d/pegellotse <<EOF
$BENUTZER ALL=(root) NOPASSWD: /usr/bin/timedatectl set-time *
$BENUTZER ALL=(root) NOPASSWD: /usr/bin/nmcli
EOF
chmod 440 /etc/sudoers.d/pegellotse
visudo -c -f /etc/sudoers.d/pegellotse >/dev/null || fehler "sudo-Regel fehlerhaft."

sage "Dienst einrichten"
cat > "/etc/systemd/system/$DIENST.service" <<EOF
[Unit]
Description=Pegellotse
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$BENUTZER
Group=$BENUTZER
SupplementaryGroups=audio
WorkingDirectory=$ZIEL
Environment=PEGELLOTSE_DATA=$DATEN
ExecStart=$ZIEL/venv/bin/python $ZIEL/pegellotse.py --port $PORT --kein-browser
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$DIENST"

if [ "${HOTSPOT:-1}" = "1" ] && [ -f "$ZIEL/hotspot.sh" ]; then
    SSID="${HOTSPOT_SSID:-Pegellotse}"
    PW="${HOTSPOT_PW:-pegellotse}"
    if [ ${#PW} -lt 8 ]; then
        echo "  HOTSPOT_PW ist zu kurz (mindestens 8 Zeichen) — uebersprungen."
    else
        sage "Notfall-Zugangspunkt einrichten ($SSID)"
        chmod +x "$ZIEL/hotspot.sh"
        # Nicht automatisch verbinden: der Zugangspunkt geht nur an, wenn
        # hotspot.sh feststellt, dass sonst nichts zustande kommt.
        nmcli connection delete hotspot >/dev/null 2>&1 || true
        nmcli connection add type wifi ifname wlan0 con-name hotspot \
            autoconnect no ssid "$SSID" \
            802-11-wireless.mode ap 802-11-wireless.band bg \
            ipv4.method shared \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PW" >/dev/null

        cat > /etc/systemd/system/pegellotse-hotspot.service <<EOF
[Unit]
Description=Zugangspunkt starten, falls kein Netz erreichbar
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
ExecStart=$ZIEL/hotspot.sh hotspot
EOF

        cat > /etc/systemd/system/pegellotse-hotspot.timer <<EOF
[Unit]
Description=Regelmaessig pruefen, ob ein Netz zustande kam

[Timer]
OnBootSec=90
OnUnitActiveSec=60
AccuracySec=5

[Install]
WantedBy=timers.target
EOF
        systemctl daemon-reload
        systemctl enable --now pegellotse-hotspot.timer
        echo "  Kommt kein Netz zustande, macht der Rechner nach etwa 90 s"
        echo "  selbst ein WLAN auf: $SSID / $PW  -> http://10.42.0.1:$PORT"
    fi
fi

if [ -n "${LUEFTER_GPIO:-}" ]; then
    sage "Temperaturgesteuerten Luefter einrichten (GPIO ${LUEFTER_GPIO})"
    TEMP_MC=$(( ${LUEFTER_TEMP:-55} * 1000 ))
    CONFIG=/boot/firmware/config.txt
    [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
    if [ -f "$CONFIG" ]; then
        sed -i '/^dtoverlay=gpio-fan/d' "$CONFIG"
        printf '\n# Luefter temperaturgesteuert (Pegellotse)\ndtoverlay=gpio-fan,gpiopin=%s,temp=%s\n' \
            "$LUEFTER_GPIO" "$TEMP_MC" >> "$CONFIG"
        echo "  eingetragen in $CONFIG — wird nach dem naechsten Neustart wirksam"
        echo "  ACHTUNG: der Luefter darf nicht direkt am GPIO haengen, sondern"
        echo "  nur ueber einen Transistor oder MOSFET. Siehe README."
    else
        echo "  config.txt nicht gefunden — Eintrag uebersprungen."
    fi
fi

if [ -n "${HOSTNAME_NEU:-}" ]; then
    sage "Rechnernamen auf $HOSTNAME_NEU setzen"
    hostnamectl set-hostname "$HOSTNAME_NEU"
    sed -i "s/127.0.1.1.*/127.0.1.1\t$HOSTNAME_NEU/" /etc/hosts || true
fi

sleep 2
if ! systemctl is-active --quiet "$DIENST"; then
    echo
    systemctl status "$DIENST" --no-pager --lines 20 || true
    fehler "Der Dienst laeuft nicht. Die Ausgabe oben nennt den Grund."
fi

NAME="$(hostname)"
sage "Fertig."
cat <<EOF
Das Dashboard laeuft und startet ab jetzt beim Einschalten mit.

  http://$NAME.local:$PORT
$(hostname -I | tr ' ' '\n' | grep -v '^$' | sed "s|^|  http://|; s|$|:$PORT|")

Mikrofon, Kalibrierung und Grenzwerte werden dort eingestellt — der Pi
braucht weder Bildschirm noch Tastatur.

Nuetzlich:
  systemctl status $DIENST        Zustand ansehen
  journalctl -u $DIENST -f        Meldungen mitlesen
  systemctl restart $DIENST       neu starten

Daten liegen in $DATEN (config.json, logs/).
EOF
