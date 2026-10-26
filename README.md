# insightful-wayland

The Insightful (Workpuls) time tracker on a Wayland desktop with the
[mango](https://github.com/DreamMaoMao/mangowc) compositor, as a
home-manager module.

The agent is an X11 app: under Xwayland it sees neither the focused window
nor real idle time. Here it runs in an Ubuntu distrobox with an LD_PRELOAD
library (`proxy.c`) that answers those calls from a shared-memory state,
written by a daemon on the host (`daemon.py`) from mango's IPC (`mmsg`: the
focused window, every input event it sees), the input devices (evdev) and
`swayidle`. A virtual input device (uinput) gives the agent the input events
it counts.

## Use

```nix
{
  inputs.insightful-wayland.url = "github:zbioe/insightful-wayland";
}
```

In the home:

```nix
{ inputs, config, ... }:
{
  imports = [ inputs.insightful-wayland.homeManagerModules.default ];
  services.insightful-wayland = {
    enable = true;
    mmsg = "${config.wayland.windowManager.mango.package}/bin/mmsg";
  };
}
```

The first activation creates the container and downloads the agent;
`insightful` (or its menu entry) starts it. Needs podman, and the user in
the `input` and `uinput` groups (evdev, uinput). With
`INSIGHTFUL_SKIP_SETUP` set, an activation leaves the setup for the next one.

`hyprland/` holds an earlier attempt for Hyprland (X11 tools, D-Bus).

## Removing it

```sh
podman rm -f insightful-app
rm -rf ~/apps/insightful.AppImage ~/apps/insightful-root ~/.config/workpuls-agent \
  ~/.local/share/insightful-proxy /dev/shm/insightful_state
```
