#!/usr/bin/env python3
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
import json
import subprocess
import time
import threading
import os
from evdev import InputDevice, ecodes
from pathlib import Path

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

os.environ["DISPLAY"] = ":0"

last_activity = time.time()
last_x11_pulse = 0 # <-- Controle para não sobrecarregar o X11

def update_activity():
    global last_activity, last_x11_pulse
    current_time = time.time()
    last_activity = current_time

    # PULSO X11 CONTROLADO: Acorda o XWayland no máximo 1 vez por segundo
    if current_time - last_x11_pulse >= 1.0:
        subprocess.Popen(["xset", "s", "reset"], stderr=subprocess.DEVNULL)
        last_x11_pulse = current_time

def get_idle_time_ms():
    return int((time.time() - last_activity) * 1000)

class IdleMonitor(dbus.service.Object):
    def __init__(self):
        bus_name = dbus.service.BusName("org.gnome.Mutter.IdleMonitor", bus=dbus.SessionBus())
        dbus.service.Object.__init__(self, bus_name, "/org/gnome/Mutter/IdleMonitor/Core")

    @dbus.service.method("org.gnome.Mutter.IdleMonitor", in_signature="", out_signature="t")
    def GetIdletime(self): return dbus.UInt64(get_idle_time_ms())

class ScreenSaver(dbus.service.Object):
    def __init__(self):
        bus_name = dbus.service.BusName("org.freedesktop.ScreenSaver", bus=dbus.SessionBus())
        dbus.service.Object.__init__(self, bus_name, "/ScreenSaver")

    @dbus.service.method("org.freedesktop.ScreenSaver", in_signature="", out_signature="b")
    def GetActive(self): return dbus.Boolean(False)

    @dbus.service.method("org.freedesktop.ScreenSaver", in_signature="", out_signature="u")
    def GetActiveTime(self): return dbus.UInt32(get_idle_time_ms() // 1000)

def mouse_event_poller():
    mouse_dev = None
    for path in Path("/dev/input").glob("event*"):
        try:
            dev = InputDevice(str(path))
            if dev.name == "INSTANT USB GAMING MOUSE ":
                mouse_dev = dev
                break
        except:
            pass

    if not mouse_dev:
        return

    while True:
        try:
            for event in mouse_dev.read_loop():
                if event.type in (ecodes.EV_KEY, ecodes.EV_REL, ecodes.EV_ABS):
                    update_activity()
        except Exception as e:
            print(f"[ERROR] Erro no mouse: {e}")
            time.sleep(0.1)
        time.sleep(0.05)

def keyboard_poller():
    devices = []
    for name in ["kanata-external", "kanata-internal"]:
        for path in Path("/dev/input").glob("event*"):
            try:
                dev = InputDevice(str(path))
                if name in dev.name.lower():
                    devices.append(dev)
                    break
            except:
                pass

    if not devices:
        return

    while True:
        for dev in devices:
            try:
                for event in dev.read_loop():
                    if event.type == ecodes.EV_KEY and event.value == 1:
                        update_activity()
            except:
                pass
        time.sleep(0.05)

def title_updater():
    while True:
        try:
            active_json = subprocess.check_output(["hyprctl", "activewindow", "-j"], text=True).strip()
            if active_json and active_json != "null":
                data = json.loads(active_json)
                title = data.get("title", "Desktop")[:255]
                wm_class = data.get("class", "Desktop")[:255] # <-- Extraindo a classe

                # Injeta o Título
                for prop in ["WM_NAME", "_NET_WM_NAME", "_NET_WM_VISIBLE_NAME"]:
                    subprocess.call(["xprop", "-root", "-set", prop, title], stderr=subprocess.DEVNULL)

                # Injeta a Classe para o sistema classificar como Produtivo/Neutro
                subprocess.call(["xprop", "-root", "-f", "WM_CLASS", "8u", "-set", "WM_CLASS", wm_class], stderr=subprocess.DEVNULL)
        except:
            pass
        time.sleep(0.2)

if __name__ == "__main__":
    update_activity()
    threading.Thread(target=mouse_event_poller, daemon=True).start()
    threading.Thread(target=keyboard_poller, daemon=True).start()
    threading.Thread(target=title_updater, daemon=True).start()
    IdleMonitor()
    ScreenSaver()
    loop = GLib.MainLoop()
    loop.run()
