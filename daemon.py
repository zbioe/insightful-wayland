#!/usr/bin/env python3
"""
insightful-daemon.py - runs on HOST (outside Distrobox)

Monitors:
  1. Focused window (title + app id) via mango's `mmsg`
  2. Input mango sees (touchpad gestures too) via `mmsg watch all-devices`
  3. Focus changes via `mmsg watch focusing-client`
  4. Creates a uinput virtual input device so tracker.node
     can read real kernel-level input events (needed for APM)

Writes to /dev/shm/insightful_state for the LD_PRELOAD hook.

SHM layout (must match insightful-proxy.c):
  int64_t last_activity  -- Unix timestamp of last activity
  int32_t idle_ms        -- reserved (0)
  int32_t force_window   -- always 1
  char    window_title[256]
  char    window_class[256]
Total: 528 bytes
"""
import fcntl
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time

# mango's IPC client (store path from home/desktop/insightful)
MMSG = os.environ.get("MMSG", "mmsg")

# ----------------------------------------------------------------
# Shared memory
# ----------------------------------------------------------------
SHM_PATH = "/dev/shm/insightful_state"

_last_activity = time.time()
_activity_lock = threading.Lock()
_shm_fd = -1       # opened once in run_daemon(), reused by write_shm()
running = True

def fail(msg):
    """A source of events is gone: the daemon stops (stale state written)
    and systemd starts it again (Restart=always)."""
    print(msg, file=sys.stderr)
    os.kill(os.getpid(), signal.SIGTERM)


def update_activity():
    global _last_activity
    with _activity_lock:
        _last_activity = time.time()

def get_last_activity():
    with _activity_lock:
        return _last_activity

def get_focused_window():
    try:
        r = subprocess.run([MMSG, "get", "focusing-client"],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout)
            if d and d.get("title"):
                return d.get("title","Desktop")[:200], (d.get("appid") or "Desktop")[:200]
    except Exception:
        pass
    return "Desktop", "Desktop"

def shm_open_once():
    """Open (or create) the SHM file once. Returns fd."""
    global _shm_fd
    if _shm_fd >= 0:
        return _shm_fd
    _shm_fd = os.open(SHM_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    # Pre-fill 528 bytes so the mmap in the C hook has valid backing pages
    os.ftruncate(_shm_fd, 528)
    print(f"[SHM] opened fd={_shm_fd}", file=sys.stderr)
    return _shm_fd

def write_shm(title, cls, ts):
    """Write state to SHM in-place (seek to 0, no truncate).
    This preserves the mmap backing pages so the C hook's
    mmap pointer stays valid for the lifetime of the file."""
    try:
        fd = shm_open_once()
        tb = title.encode("utf-8","replace")[:255]
        cb = cls.encode("utf-8","replace")[:255]
        t256 = tb + b"\x00"*(256-len(tb))
        c256 = cb + b"\x00"*(256-len(cb))
        data = struct.pack("<qii256s256s", int(ts), 0, 1, t256, c256)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)
    except Exception as e:
        print(f"[SHM] write error: {e}", file=sys.stderr)

# ----------------------------------------------------------------
# uinput virtual device
# ----------------------------------------------------------------
UINPUT_PATH   = "/dev/uinput"
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY= 0x5502
UI_SET_EVBIT  = 0x40045564
UI_SET_RELBIT = 0x40045566
UI_DEV_SETUP  = 0x405c5503
EV_SYN = 0
EV_REL = 2
REL_X = 0

_uinput_fd = None
_uinput_lock = threading.Lock()

def uinput_open():
    global _uinput_fd
    try:
        fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
        fcntl.ioctl(fd, UI_SET_RELBIT, REL_X)
        name = b"insightful-virtual-mouse" + b"\x00"*56   # 80 bytes total
        setup = struct.pack("=HHHH80sI", 0x06, 0, 0, 1, name, 0)
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
        _uinput_fd = fd
        print("[uinput] Virtual mouse created", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[uinput] Cannot create device: {e}", file=sys.stderr)
        return False

def uinput_close():
    global _uinput_fd
    if _uinput_fd is not None:
        try:
            fcntl.ioctl(_uinput_fd, UI_DEV_DESTROY)
            os.close(_uinput_fd)
        except Exception:
            pass
        _uinput_fd = None

def uinput_inject():
    """Write a REL_X + SYN event to the virtual device."""
    with _uinput_lock:
        if _uinput_fd is None:
            return
        try:
            tv = int(time.time())
            # struct input_event: long tv_sec, long tv_usec, u16 type, u16 code, s32 value
            rel = struct.pack("=qqHHi", tv, 0, EV_REL, REL_X, 1)
            syn = struct.pack("=qqHHi", tv, 0, EV_SYN, 0,     0)
            os.write(_uinput_fd, rel)
            os.write(_uinput_fd, syn)
        except Exception as e:
            print(f"[uinput] inject error: {e}", file=sys.stderr)

# ----------------------------------------------------------------
# Activity monitors
# ----------------------------------------------------------------
# How long to keep injecting uinput events after last detected activity.
# Matches the tracker's idle_setting (120s) from the server config.
ACTIVE_TIMEOUT = 120

# Linux input event types we care about
EV_KEY_T = 1    # keyboard key / mouse button
EV_REL_T = 2    # mouse relative movement / scroll wheel

def evdev_monitor():
    """Read real /dev/input devices for keyboard, mouse buttons, and scroll.
    This detects ALL physical user input: key presses, mouse clicks,
    scroll wheel, and mouse movement via the kernel evdev interface.
    Runs on the HOST where we have the input group."""
    import select as sel
    fds = []
    fd_names = {}

    # Scan all input devices and open ones that support KEY or REL events
    for i in range(30):
        path = f"/dev/input/event{i}"
        if not os.path.exists(path):
            continue
        # Skip our own virtual device
        try:
            name_path = f"/sys/class/input/event{i}/device/name"
            if os.path.exists(name_path):
                dname = open(name_path).read().strip()
                if "insightful" in dname.lower():
                    continue
                # Skip audio/video/power devices (only want HID)
                if any(x in dname.lower() for x in ("hdmi", "audio", "video", "power", "mic", "headphone")):
                    continue
        except Exception:
            pass

        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            # Check if device supports EV_KEY or EV_REL using EVIOCGBIT
            import fcntl as fc
            buf = bytearray(8)
            EVIOCGBIT_0 = (2 << 30) | (ord("E") << 8) | 0x20 | (8 << 16)
            fc.ioctl(fd, EVIOCGBIT_0, buf)
            bits = struct.unpack("<Q", bytes(buf))[0]
            has_key = bool(bits & (1 << EV_KEY_T))
            has_rel = bool(bits & (1 << EV_REL_T))
            if has_key or has_rel:
                fds.append(fd)
                fd_names[fd] = path
            else:
                os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass

    if not fds:
        print("[evdev] No suitable input devices found", file=sys.stderr)
        return

    print(f"[evdev] Monitoring {len(fds)} devices: {list(fd_names.values())}", file=sys.stderr)

    while running:
        try:
            readable, _, _ = sel.select(fds, [], [], 1.0)
            for fd in readable:
                try:
                    while True:
                        data = os.read(fd, 72)  # up to 3 events at once
                        if len(data) < 24:
                            break
                        for off in range(0, len(data) - 23, 24):
                            chunk = data[off:off+24]
                            _, _, ev_type, _, _ = struct.unpack("=qqHHi", chunk)
                            if ev_type in (EV_KEY_T, EV_REL_T):
                                update_activity()
                                break  # one update per batch is enough
                except (BlockingIOError, OSError):
                    pass
        except Exception as e:
            # (a device gone: the restart finds the ones there now)
            fail(f"[evdev] error: {e}")

    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass

def device_events():
    """Count every input event mango reports as activity: the ones evdev
    can't see (e.g. touchpad gestures routed through Wayland only)."""
    print("[mango] watching input devices", file=sys.stderr)
    with subprocess.Popen([MMSG, "watch", "all-devices"],
                          stdout=subprocess.PIPE, text=True) as p:
        for line in p.stdout:
            if not running:
                p.terminate()
                return
            update_activity()
    fail("[mango] the input stream ended (mango restarting?)")

def swayidle_monitor():
    """Use swayidle (ext-idle-notify Wayland protocol) to detect
    when user resumes from idle. This catches ALL input types
    including keyboard, mouse clicks, and scroll - the only
    reliable method on Wayland since evdev is blocked."""
    import shutil
    swayidle_bin = shutil.which("swayidle")
    if not swayidle_bin:
        print("[swayidle] Not found, skipping", file=sys.stderr)
        return
    print("[swayidle] Starting idle monitor...", file=sys.stderr)
    with subprocess.Popen(
        [swayidle_bin, "-w",
         "timeout", "3", "echo idle",
         "resume", "echo resume"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True
    ) as proc:
        for line in proc.stdout:
            if not running:
                proc.terminate()
                return
            if line.strip() == "resume":
                update_activity()
    fail("[swayidle] it ended")

def activity_injector():
    """Continuously inject uinput events while user is active.
    The main loop handles SHM writes with the effective timestamp."""
    print("[injector] Starting...", file=sys.stderr)
    # (every half second: the tracker counts input events over time)
    while running:
        try:
            idle = time.time() - get_last_activity()
            if idle < ACTIVE_TIMEOUT:
                uinput_inject()
        except Exception as e:
            fail(f"[injector] error: {e}")
            return
        time.sleep(0.5)

def focus_events():
    """Count focus changes (window switch, tag switch) as activity."""
    print("[mango] watching the focused client", file=sys.stderr)
    with subprocess.Popen([MMSG, "watch", "focusing-client"],
                          stdout=subprocess.PIPE, text=True) as p:
        last = None
        for line in p.stdout:
            if not running:
                p.terminate()
                return
            try:
                d = json.loads(line)
                key = (d.get("id"), d.get("title"), tuple(d.get("tags") or ()))
            except Exception:
                continue
            if last is not None and key != last:
                update_activity()
                uinput_inject()
            last = key
    fail("[mango] the focus stream ended (mango restarting?)")

# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
def run_daemon():
    global running
    print("[insightful-daemon] Starting...", file=sys.stderr)

    uinput_open()

    for target in (evdev_monitor, device_events, focus_events,
                  swayidle_monitor, activity_injector):
        threading.Thread(target=target, daemon=True).start()

    def cleanup(sig=None, frame=None):
        global running, _shm_fd
        running = False
        print("[insightful-daemon] Shutting down...", file=sys.stderr)
        uinput_close()
        # Write a stale timestamp (epoch 0) so the C hook reports high
        # idle time, but do NOT unlink the file -- if Workpuls is still
        # running, its mmap must remain valid for a future daemon restart.
        try:
            if _shm_fd >= 0:
                data = struct.pack("<qii256s256s",
                    0, 0, 0,
                    b"\x00"*256,
                    b"\x00"*256)
                os.lseek(_shm_fd, 0, os.SEEK_SET)
                os.write(_shm_fd, data)
                os.close(_shm_fd)
                _shm_fd = -1
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT,  cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print(f"[insightful-daemon] SHM={SHM_PATH}", file=sys.stderr)

    while running:
        try:
            title, cls = get_focused_window()
            # Use the "effective" timestamp: if within ACTIVE_TIMEOUT
            # of last real activity, report current time (user active).
            # Otherwise report the actual last_activity (user idle).
            real_la = get_last_activity()
            idle = time.time() - real_la
            effective_ts = time.time() if idle < ACTIVE_TIMEOUT else real_la
            write_shm(title, cls, effective_ts)
        except Exception as e:
            print(f"[daemon] error: {e}", file=sys.stderr)
        # (the effective time moves on by itself while active: refreshed)
        time.sleep(0.5)

if __name__ == "__main__":
    run_daemon()
