# Insightful (Workpuls) on a Wayland desktop with mango: the agent runs in an
# Ubuntu distrobox under Xwayland, and an LD_PRELOAD library (proxy.c) gives
# it what it can't read there, the focused window and the idle time, from a
# daemon on the host (daemon.py) that follows mango (mmsg) and the input
# devices.
{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.services.insightful-wayland;
  python = pkgs.python312;
  home = config.home.homeDirectory;
  appImage = "${home}/apps/insightful.AppImage";
  appDir = "${home}/apps/insightful-root";
  inherit (config.home) username;

  desktop = pkgs.makeDesktopItem {
    name = "insightful";
    desktopName = "Insightful";
    exec = "${home}/.local/bin/insightful";
    icon = "${appDir}/Workpuls.png";
    categories = [
      "Utility"
      "Office"
    ];
    comment = "Insightful time tracking";
    terminal = false;
  };
in
{
  options.services.insightful-wayland = {
    enable = lib.mkEnableOption "Insightful (Workpuls) in a distrobox, with mango's window and idle state";
    mmsg = lib.mkOption {
      type = lib.types.str;
      default = "mmsg";
      example = lib.literalExpression ''"''${config.wayland.windowManager.mango.package}/bin/mmsg"'';
      description = "mango's IPC client, which the daemon follows.";
    };
    container = lib.mkOption {
      type = lib.types.str;
      default = "insightful-app";
      description = "The distrobox container the agent runs in.";
    };
    image = lib.mkOption {
      type = lib.types.str;
      default = "ubuntu:22.04";
      description = "Its image.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [
      python
      pkgs.swayidle
      desktop
    ];

    home.file.".local/share/insightful-proxy/insightful-proxy.c".source = ./proxy.c;
    home.file.".local/share/insightful-proxy/insightful-daemon.py" = {
      executable = true;
      source = ./daemon.py;
    };

    # the container and the app, once (each step checks first); skipped when
    # INSIGHTFUL_SKIP_SETUP is set (an activation at boot: minutes of downloads)
    home.activation.insightfulContainer = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
      if [ -n "''${INSIGHTFUL_SKIP_SETUP:-}" ]; then
        echo "[Insightful] setup skipped"
      else
        CONTAINER="${cfg.container}"
        IMAGE="${cfg.image}"
        APPIMAGE="${appImage}"
        APPDIR="${appDir}"
        USERNAME="${username}"
        DOCKER=podman
        DISTROBOX="${pkgs.distrobox}/bin/distrobox"
        CURL="${pkgs.curl}/bin/curl"
        # distrobox needs standard utils (awk, rev, sed, grep, etc.)
        export DBX_CONTAINER_MANAGER=podman
        export PATH="/run/wrappers/bin:${pkgs.gawk}/bin:${pkgs.util-linux}/bin:${pkgs.gnused}/bin:${pkgs.gnugrep}/bin:${pkgs.coreutils}/bin:${pkgs.findutils}/bin:/run/current-system/sw/bin:''$PATH"

        # 1. the distrobox container
        if ! ''$DOCKER ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "''$CONTAINER"; then
          echo "[Insightful] Creating distrobox container ''$CONTAINER..."
          ''$DISTROBOX create --yes --image "''$IMAGE" --name "''$CONTAINER" \
            --additional-packages "gcc libx11-dev libxss-dev libxext-dev libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libpango-1.0-0 libcairo2"
          # the first enter maps the user, mounts and installs the packages
          ''$DISTROBOX enter "''$CONTAINER" -- true
        fi

        # 2. build deps and the input group (with the container running)
        if ''$DOCKER ps --format '{{.Names}}' 2>/dev/null | grep -qx "''$CONTAINER"; then
          if ! ''$DOCKER exec "''$CONTAINER" dpkg -l gcc libnss3 2>/dev/null | grep -cq '^ii' | grep -q 2; then
            ''$DOCKER exec --user root "''$CONTAINER" \
              bash -c 'apt-get update -qq && apt-get install -y -qq gcc libx11-dev libxss-dev libxext-dev libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libpango-1.0-0 libcairo2' \
              2>/dev/null || true
          fi
          if ! ''$DOCKER exec "''$CONTAINER" bash -c "getent group 174 2>/dev/null | grep -q ''$USERNAME" 2>/dev/null; then
            ''$DOCKER exec --user root "''$CONTAINER" bash -c "
              groupadd -g 174 input 2>/dev/null || true
              usermod -aG input ''$USERNAME 2>/dev/null || true
            " 2>/dev/null || true
          fi
        fi

        # 3. the AppImage, unless the app is there
        if [ ! -f "''$APPIMAGE" ] && [ ! -x "''$APPDIR/Workpuls" ]; then
          echo "[Insightful] Downloading Workpuls AppImage..."
          mkdir -p "$(dirname "''$APPIMAGE")"
          ''$CURL -L --progress-bar \
            "https://insightful-updates.io/linux/agent/latest/Workpuls.AppImage" \
            -o "''$APPIMAGE"
          chmod +x "''$APPIMAGE"
        fi

        # 4. extracted
        if [ ! -x "''$APPDIR/Workpuls" ] && [ -f "''$APPIMAGE" ]; then
          APPIMAGE_DIR="$(dirname "''$APPIMAGE")"
          cd "''$APPIMAGE_DIR"
          "''$APPIMAGE" --appimage-extract >/dev/null 2>&1 || true
          if [ -d "''$APPIMAGE_DIR/squashfs-root" ]; then
            mv "''$APPIMAGE_DIR/squashfs-root" "''$APPDIR"
          else
            echo "[Insightful] WARNING: AppImage extraction failed."
          fi
        fi
      fi
    '';

    home.file.".local/bin/insightful" = {
      executable = true;
      text = ''
        #!/usr/bin/env bash
        set -euo pipefail

        # the container running
        export DBX_CONTAINER_MANAGER=podman
        export PATH="/run/wrappers/bin:${pkgs.gawk}/bin:${pkgs.util-linux}/bin:/run/current-system/sw/bin:$PATH"
        if ! podman ps --format '{{.Names}}' 2>/dev/null | grep -qx '${cfg.container}'; then
            distrobox enter ${cfg.container} -- true
        fi

        LIB_DIR="$HOME/.local/share/insightful-proxy"
        LIB_NAME="libinsightful-proxy.so"
        SRC="$LIB_DIR/insightful-proxy.c"

        SRC_HASH="$(md5sum "$SRC" 2>/dev/null | cut -d' ' -f1)"
        HASH_FILE="$LIB_DIR/lib/.src_hash"
        PREV_HASH="$(cat "$HASH_FILE" 2>/dev/null || true)"

        # the library, built in the container (its X11 libraries)
        if [ ! -f "$LIB_DIR/lib/$LIB_NAME" ] || [ "$SRC_HASH" != "$PREV_HASH" ]; then
            distrobox enter ${cfg.container} -- bash -c "
                set -e
                mkdir -p $LIB_DIR/lib
                gcc -shared -fPIC -O2 -o $LIB_DIR/lib/$LIB_NAME $SRC -lX11 -lXss -ldl -lpthread
            "
            echo "$SRC_HASH" > "$HASH_FILE"
        fi

        exec distrobox enter ${cfg.container} -- bash -c "
            export LD_PRELOAD='$LIB_DIR/lib/$LIB_NAME'
            unset GIO_EXTRA_MODULES
            cd \"\$HOME/apps/insightful-root\"
            exec ./Workpuls --no-sandbox >/dev/null 2>&1
        "
      '';
    };

    systemd.user.services.insightful-proxy-daemon = {
      Unit = {
        Description = "Insightful Proxy - mango window + activity bridge for Workpuls";
        PartOf = [ "graphical-session.target" ];
        After = [ "graphical-session.target" ];
      };
      Service = {
        ExecStart = "${python}/bin/python %h/.local/share/insightful-proxy/insightful-daemon.py";
        # (a source of events gone: daemon.py exits, started again)
        Restart = "always";
        RestartSec = 3;
        Environment = [
          "DISPLAY=:0"
          "MMSG=${cfg.mmsg}"
        ];
      };
      Install.WantedBy = [ "graphical-session.target" ];
    };
  };
}
