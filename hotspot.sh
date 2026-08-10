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
# hotspot.sh — macht den Rechner selbst zum Zugangspunkt, wenn er sonst in
# keinem Netz landet. Ohne das steht man am Veranstaltungsort vor einem Pi
# ohne Bildschirm, der sich nirgends eingebucht hat.
#
# Wird von pegellotse-hotspot.timer regelmaessig aufgerufen.
#
# Absicht:
#   - laeuft eine Verbindung ueber Kabel oder WLAN, passiert nichts
#   - sonst wird der Zugangspunkt gestartet
#   - laeuft er bereits, bleibt er an. Die WLAN-Chips im Pi koennen nicht
#     gleichzeitig senden und nach fremden Netzen suchen; ein automatisches
#     Zurueckwechseln waere also ein staendiges Auf und Zu. Beendet wird der
#     Zugangspunkt im Dashboard oder durch einen Neustart.
#
set -euo pipefail

VERBINDUNG="${1:-hotspot}"

aktiv() { nmcli -t -f NAME connection show --active | grep -Fxq "$1"; }

# Laeuft der Zugangspunkt schon? Dann nichts tun.
aktiv "$VERBINDUNG" && exit 0

# Irgendeine echte Verbindung vorhanden?
while IFS=: read -r name typ geraet; do
    case "$typ" in
        802-3-ethernet|802-11-wireless)
            [ "$name" = "$VERBINDUNG" ] && continue
            [ -n "${geraet:-}" ] && exit 0
            ;;
    esac
done < <(nmcli -t -f NAME,TYPE,DEVICE connection show --active)

logger -t pegellotse-hotspot "Kein Netz gefunden — starte Zugangspunkt $VERBINDUNG"
nmcli connection up "$VERBINDUNG"
