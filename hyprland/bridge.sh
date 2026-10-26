#!/usr/bin/env bash

APP_CLASS="workpuls-agent"

while [ -z "$INS_ID" ]; do
    INS_ID=$(xdotool search --class "$APP_CLASS" | head -n 1)
    sleep 2
done

last_title=""
while true; do
    TITLE=$(hyprctl activewindow -j | jq -r '.title // "Desktop"')

    if [ "$TITLE" != "$last_title" ]; then
        xprop -id "$INS_ID" -f WM_NAME 8u -set WM_NAME "$TITLE"
        xprop -id "$INS_ID" -f _NET_WM_NAME 8u -set _NET_WM_NAME "$TITLE"
        last_title="$TITLE"
    fi

    xdotool mousemove_relative 0 0
    sleep 2
done
