#!/usr/bin/env bash
#
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
#
# wlan.sh — Zugangspunkt und Netzwechsel auf dem Raspberry Pi.
#
# Laeuft als root: vom Timer (waechter) oder ueber eine enge sudo-Regel aus
# dem Dashboard. Alle Eingriffe ins Netz gehen hier durch.
#
# Ein oder zwei WLAN-Chips:
#   - Nur der eingebaute Chip: er kann entweder Zugangspunkt sein ODER sich
#     in ein fremdes Netz einbuchen und nach Netzen suchen. Deshalb wird vor
#     dem Start des Zugangspunkts gesucht und die Liste gemerkt, und ein
#     Netzwechsel faellt bei Misserfolg von selbst auf den Zugangspunkt zurueck.
#   - Steckt zusaetzlich ein USB-WLAN-Stick: ein Chip macht dauerhaft den
#     Zugangspunkt, der andere sucht und verbindet sich frei. Der eingebaute
#     Chip wird bevorzugt Zugangspunkt (zuverlaessig), der Stick bucht sich
#     ein (dafuer taugt praktisch jeder Stick, oft mit besserer Antenne).
#
# Befehle:
#   waechter          vom Timer: Zugangspunkt starten, wenn noetig
#   hotspot-an        Zugangspunkt starten (abgekoppelt)
#   hotspot-aus       Zugangspunkt beenden
#   wechsel NAME      mit gespeichertem Netz NAME verbinden, bei Misserfolg
#                     zurueck zum vorherigen Zustand (abgekoppelt)
#   suchen            nur ein Chip und Zugangspunkt aktiv: kurz abschalten,
#                     suchen, wieder einschalten (abgekoppelt)
#
set -uo pipefail

HOTSPOT="hotspot"
DATEN_STANDARD=/var/lib/pegellotse
DATEN="${PEGELLOTSE_DATA:-$DATEN_STANDARD}"
SCAN="$DATEN/wlan-scan.txt"
STATUS="$DATEN/wlan-wechsel.txt"
# Liegt nur im Arbeitsspeicher: nach einem Neustart ist der Zugangspunkt
# wieder erwuenscht.
AUS_MARKE=/run/pegellotse-hotspot-aus
SELBST="$(readlink -f "$0")"

log() { logger -t pegellotse-wlan "$*"; echo "$*"; }

# ---------------------------------------------------------------- Geraete
wlan_geraete() {
    nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2=="wifi"{print $1}'
}

kann_ap() {
    [ "$(nmcli -g WIFI-PROPERTIES.AP device show "$1" 2>/dev/null)" = "yes" ]
}

ist_usb() {
    readlink -f "/sys/class/net/$1/device" 2>/dev/null | grep -q '/usb'
}

verbindung_auf() {   # aktive Verbindung auf einem Geraet (leer = keine)
    nmcli -g GENERAL.CONNECTION device show "$1" 2>/dev/null | sed 's/^--$//'
}

hotspot_geraet() {
    nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
        | awk -F: -v n="$HOTSPOT" '$1==n{print $2; exit}'
}

anzahl_geraete() { wlan_geraete | grep -c .; }

# Geraet fuer den Zugangspunkt: frei und AP-faehig, eingebauter Chip zuerst.
frei_und_ap() { [ -z "$(verbindung_auf "$1")" ] && kann_ap "$1"; }

ap_kandidat() {
    local g
    for g in $(wlan_geraete); do
        ist_usb "$g" && continue
        frei_und_ap "$g" && { echo "$g"; return; }
    done
    for g in $(wlan_geraete); do
        ist_usb "$g" || continue
        frei_und_ap "$g" && { echo "$g"; return; }
    done
}

# Geraet zum Suchen und Einbuchen: alles ausser dem Zugangspunkt.
client_geraet() {
    local ap g
    ap="$(hotspot_geraet)"
    for g in $(wlan_geraete); do [ "$g" != "$ap" ] && [ -n "$(verbindung_auf "$g")" ] && { echo "$g"; return; }; done
    for g in $(wlan_geraete); do [ "$g" != "$ap" ] && { echo "$g"; return; }; done
}

irgendein_netz() {   # echte Verbindung ueber Kabel oder WLAN (nicht der AP)
    local name typ geraet
    while IFS=: read -r name typ geraet; do
        case "$typ" in
            802-3-ethernet|802-11-wireless)
                [ "$name" = "$HOTSPOT" ] && continue
                [ -n "${geraet:-}" ] && return 0 ;;
        esac
    done < <(nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null)
    return 1
}

# ---------------------------------------------------------------- Dateien
fuer_dienst() {   # Datei dem Dienstbenutzer ueberlassen
    chown --reference="$DATEN" "$1" 2>/dev/null || true
    chmod 644 "$1" 2>/dev/null || true
}

scan_merken() {   # $1 = Geraet; Ergebnis im nmcli-Format fuer das Dashboard
    local tmp="$SCAN.tmp"
    [ -d "$DATEN" ] || return 0
    nmcli -t -f SSID,SIGNAL,SECURITY device wifi list ifname "$1" --rescan yes \
        > "$tmp" 2>/dev/null || true
    if [ -s "$tmp" ]; then mv -f "$tmp" "$SCAN"; fuer_dienst "$SCAN"
    else rm -f "$tmp"; fi
}

status() {   # zustand|ziel|unix-zeit|text
    [ -d "$DATEN" ] || return 0
    printf '%s|%s|%s|%s\n' "$1" "$2" "$(date +%s)" "$3" > "$STATUS.tmp"
    mv -f "$STATUS.tmp" "$STATUS"; fuer_dienst "$STATUS"
}

# ---------------------------------------------------------------- Zugangspunkt
hotspot_starten() {
    local g
    g="$(hotspot_geraet)"
    [ -n "$g" ] && return 0
    g="$(ap_kandidat)"
    if [ -z "$g" ]; then
        # Nur ein Chip, und der ist gerade eingebucht: den nehmen.
        g="$(wlan_geraete | head -n1)"
        [ -n "$g" ] || { log "Kein WLAN-Geraet vorhanden."; return 1; }
        kann_ap "$g" || { log "$g kann keinen Zugangspunkt."; return 1; }
    fi
    # Aeltere Einrichtungen haben das Profil fest an wlan0 gebunden.
    nmcli connection modify "$HOTSPOT" connection.interface-name "" >/dev/null 2>&1 || true
    # Frei? Dann vorher noch einmal umsehen — danach ist das Suchen vorbei,
    # solange nur ein Chip da ist.
    [ -z "$(verbindung_auf "$g")" ] && scan_merken "$g"
    log "Starte Zugangspunkt auf $g"
    nmcli connection up "$HOTSPOT" ifname "$g" >/dev/null
}

waechter() {
    local n
    n="$(anzahl_geraete)"
    [ "$n" -gt 0 ] || exit 0
    [ -n "$(hotspot_geraet)" ] && exit 0
    if [ "$n" -ge 2 ]; then
        # Zwei Chips: der Zugangspunkt kostet nichts und bleibt an — ausser
        # er wurde im Dashboard bewusst beendet.
        [ -e "$AUS_MARKE" ] && exit 0
        [ -n "$(ap_kandidat)" ] || exit 0
        hotspot_starten
    else
        irgendein_netz && exit 0
        log "Kein Netz gefunden"
        hotspot_starten
    fi
}

# ---------------------------------------------------------------- Abkoppeln
# Der Aufruf kommt aus einer HTTP-Anfrage, deren Verbindung gleich reissen
# kann. Deshalb laeuft die eigentliche Arbeit als eigene systemd-Einheit.
abkoppeln() {
    local einheit="pegellotse-wlan-$1"; shift
    systemctl stop "$einheit" >/dev/null 2>&1 || true
    systemctl reset-failed "$einheit" >/dev/null 2>&1 || true
    systemd-run --quiet --collect --unit="$einheit" \
        --setenv=PEGELLOTSE_DATA="$DATEN" "$SELBST" "$@" >/dev/null
}

# ---------------------------------------------------------------- Wechsel
wechsel_ausfuehren() {
    local ziel="$1" g vorher geraete_zahl
    sleep 2   # die HTTP-Antwort soll noch hinausgehen
    geraete_zahl="$(anzahl_geraete)"
    g="$(client_geraet)"
    if [ -z "$g" ]; then
        # Nur ein Chip, und der macht den Zugangspunkt
        g="$(hotspot_geraet)"
    fi
    [ -n "$g" ] || { status fehler "$ziel" "Kein WLAN-Geraet gefunden."; return 1; }
    vorher="$(verbindung_auf "$g")"
    status laeuft "$ziel" "Verbinde über $g …"
    log "Wechsel auf $ziel ueber $g (vorher: ${vorher:-nichts})"

    if [ "$vorher" = "$HOTSPOT" ] && [ "$geraete_zahl" -lt 2 ]; then
        nmcli connection down "$HOTSPOT" >/dev/null 2>&1 || true
        sleep 2
    fi

    # Ein fest an ein anderes Geraet gebundenes Profil liesse sich hier nicht
    # aktivieren (etwa das "preconfigured" aus dem Raspberry Pi Imager).
    local gebunden
    gebunden="$(nmcli -g connection.interface-name connection show "$ziel" 2>/dev/null)"
    [ -n "$gebunden" ] && [ "$gebunden" != "$g" ] && \
        nmcli connection modify "$ziel" connection.interface-name "" >/dev/null 2>&1

    local ausgabe
    if ausgabe="$(nmcli --wait 40 connection up "$ziel" ifname "$g" 2>&1)"; then
        status ok "$ziel" "Verbunden mit $ziel."
        log "Verbunden mit $ziel"
        rm -f "$AUS_MARKE"
        # Zwei Chips: Zugangspunkt sicherstellen
        [ "$geraete_zahl" -ge 2 ] && { hotspot_starten || true; }
        return 0
    fi

    ausgabe="$(printf '%s' "$ausgabe" | tr '\n|' '  ' | cut -c1-200)"
    log "Wechsel auf $ziel fehlgeschlagen: $ausgabe"
    nmcli connection down "$ziel" >/dev/null 2>&1 || true
    local zurueck="zurück zum Zugangspunkt"
    if [ -n "$vorher" ] && [ "$vorher" != "$HOTSPOT" ] \
        && nmcli --wait 30 connection up "$vorher" ifname "$g" >/dev/null 2>&1; then
        zurueck="zurück in $vorher"
    else
        hotspot_starten || zurueck="Zugangspunkt ließ sich nicht starten"
    fi
    status fehler "$ziel" "Verbindung mit $ziel gescheitert ($ausgabe) — $zurueck."
    return 1
}

# ---------------------------------------------------------------- Suchen
suchen_ausfuehren() {
    local g
    sleep 2
    g="$(hotspot_geraet)"
    if [ -z "$g" ] || [ "$(anzahl_geraete)" -ge 2 ]; then
        g="$(client_geraet)"; [ -n "$g" ] && scan_merken "$g"; return 0
    fi
    log "Kurz ohne Zugangspunkt, um auf $g zu suchen"
    nmcli connection down "$HOTSPOT" >/dev/null 2>&1 || true
    sleep 3
    scan_merken "$g"
    # Findet der Rechner dabei ein bekanntes Netz, darf er es behalten.
    for _ in $(seq 1 10); do
        [ -n "$(verbindung_auf "$g")" ] && [ "$(verbindung_auf "$g")" != "$HOTSPOT" ] && {
            log "Dabei eingebucht in $(verbindung_auf "$g")"; return 0; }
        sleep 1
    done
    hotspot_starten
}

# ---------------------------------------------------------------- Ablauf
command -v nmcli >/dev/null || { echo "nmcli fehlt" >&2; exit 1; }

# Nie zwei Eingriffe gleichzeitig — etwa der Timer mitten in einem Wechsel.
sperren() { exec 9>/run/pegellotse-wlan.lock; flock "$@" 9; }

case "${1:-}" in
    waechter)            sperren -n || exit 0; waechter ;;
    hotspot-an)          rm -f "$AUS_MARKE"; abkoppeln hotspot _hotspot-an ;;
    _hotspot-an)         sleep 2; sperren -w 120; hotspot_starten ;;
    hotspot-aus)         touch "$AUS_MARKE"
                         nmcli connection down "$HOTSPOT" >/dev/null ;;
    wechsel)             [ -n "${2:-}" ] || { echo "Netzname fehlt" >&2; exit 2; }
                         nmcli -t -f NAME connection show | grep -Fxq -- "$2" \
                             || { echo "Kein gespeichertes Netz namens $2" >&2; exit 2; }
                         [ "$2" = "$HOTSPOT" ] && { echo "Das ist der Zugangspunkt" >&2; exit 2; }
                         status laeuft "$2" "Wechsel wird vorbereitet …"
                         abkoppeln wechsel _wechsel "$2" ;;
    _wechsel)            sperren -w 120; wechsel_ausfuehren "$2" ;;
    suchen)              abkoppeln suchen _suchen ;;
    _suchen)             sperren -w 120; suchen_ausfuehren ;;
    *)                   echo "Aufruf: $0 waechter|hotspot-an|hotspot-aus|wechsel NAME|suchen" >&2
                         exit 2 ;;
esac
