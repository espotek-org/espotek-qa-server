#!/bin/bash
# Turn this MacBook into a Labrador QA tester. Run it from a checkout of this
# repo, as an admin account that can sudo, with the Labrador plugged in.
# Idempotent: rerun it after `git pull` to deploy changes.
#
#   ./bootstrap.sh --tunnel-credentials <file.json>   # first run on a new machine
#   ./bootstrap.sh                                    # later runs
#
# The tunnel credentials file comes from `cloudflared tunnel create` (see
# README.md). Moving the tester to another MacBook: copy
# /Users/qaweb/.cloudflared/<tunnel-id>.json from the old tester and pass it here.
set -euo pipefail

QA_HOME=/Users/Shared/espotek-qa
WEB_USER=qaweb
MAC_USER=qamac
WEB_PORT=8890
HOSTNAME_PUBLIC=qa-results.espotek.com
ADMIN_USER=$(id -un)
REPO_DIR=$(cd "$(dirname "$0")" && pwd)
TUNNEL_CREDS=""
SKIP_VMS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --tunnel-credentials) TUNNEL_CREDS=$2; shift 2 ;;
    --skip-vms) SKIP_VMS=1; shift ;;  # build golden images later with `qa vm build <platform>`
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

T0=$(date +%s)
step() { printf '\n==> [%4ss] %s\n' "$(( $(date +%s) - T0 ))" "$*"; }
die() { echo "bootstrap: error: $*" >&2; exit 1; }
# Random hex secret of $1 characters (finite output, so no SIGPIPE under pipefail).
rand() { openssl rand -hex 64 | cut -c "1-$1"; }

[ "$(uname -s)" = Darwin ] || die "this is for macOS hosts"
[ "$ADMIN_USER" != root ] || die "run as an admin account, not root"
sudo -n true 2>/dev/null || sudo -v || die "needs sudo"

step "Power: never sleep, restart after power loss"
sudo pmset -a sleep 0 disksleep 0 womp 1 autorestart 1

step "Xcode Command Line Tools"
if ! xcode-select -p >/dev/null 2>&1; then
  sudo touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
  label=$(softwareupdate -l 2>/dev/null | sed -n 's/^\* Label: \(Command Line Tools for Xcode-.*\)$/\1/p' | sort -V | tail -1)
  [ -n "$label" ] || die "softwareupdate offers no Command Line Tools package"
  echo "installing $label (a large download)"
  sudo softwareupdate -i "$label" --verbose
  sudo rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
fi
xcode-select -p

step "MacPorts (Homebrew no longer installs on Intel Macs, so this project uses MacPorts on every host)"
if [ ! -x /opt/local/bin/port ]; then
  major=$(sw_vers -productVersion | cut -d. -f1)
  case "$major" in
    13) name=Ventura ;; 14) name=Sonoma ;; 15) name=Sequoia ;; 26) name=Tahoe ;;
    *) die "no MacPorts installer mapping for macOS $major; add it here" ;;
  esac
  ver=$(curl -fsSL https://api.github.com/repos/macports/macports-base/releases/latest | sed -n 's/.*"tag_name": "v\([0-9.]*\)".*/\1/p')
  [ -n "$ver" ] || die "could not find the latest MacPorts version"
  pkg="MacPorts-$ver-$major-$name.pkg"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/$pkg" "https://github.com/macports/macports-base/releases/download/v$ver/$pkg"
  pkgutil --check-signature "$tmp/$pkg" | grep -q "trusted by the Apple notary service" || die "$pkg is not notarized"
  sudo installer -pkg "$tmp/$pkg" -target /
  rm -rf "$tmp"
fi
sudo /opt/local/bin/port -q selfupdate

step "QEMU and cloudflared from MacPorts (QEMU builds from source on Intel; expect most of an hour the first time)"
sudo /opt/local/bin/port -N install qemu cloudflared
/opt/local/bin/qemu-system-x86_64 --version | head -1
/opt/local/bin/cloudflared --version
# HVF needs the hypervisor entitlement on the QEMU binary. Re-sign ad hoc when a
# port (re)install left it without one; this reruns after every `port upgrade`.
if ! codesign -d --entitlements - /opt/local/bin/qemu-system-x86_64 2>/dev/null | grep -q com.apple.security.hypervisor; then
  ent=$(mktemp)
  cat > "$ent" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>com.apple.security.hypervisor</key><true/></dict></plist>
PLIST
  sudo codesign --force --sign - --entitlements "$ent" /opt/local/bin/qemu-system-x86_64
  rm -f "$ent"
  echo "signed qemu-system-x86_64 with the hypervisor entitlement"
fi

step "uv (shared by every account)"
if [ ! -x /usr/local/bin/uv ]; then
  sudo mkdir -p /usr/local/bin
  curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
/usr/local/bin/uv --version

step "Directories under $QA_HOME"
sudo mkdir -p "$QA_HOME"
sudo chown "$ADMIN_USER":staff "$QA_HOME"
chmod 755 "$QA_HOME"
mkdir -p "$QA_HOME"/{app,python,results,state,logs,cache,work}
mkdir -p -m 700 "$QA_HOME"/{secrets,ssh,vms}
chmod 755 "$QA_HOME"/{app,python,results,state,logs,cache,work}
touch /Users/Shared/labrador-board.lock
chmod 666 /Users/Shared/labrador-board.lock

step "Deploy this checkout to $QA_HOME/app"
rsync -a --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude config.toml \
  "$REPO_DIR"/ "$QA_HOME/app/"
chmod -R go+rX "$QA_HOME/app"
(cd "$QA_HOME/app" && UV_PYTHON_INSTALL_DIR="$QA_HOME/python" UV_CACHE_DIR="$QA_HOME/cache/uv" \
  /usr/local/bin/uv sync --quiet --python 3.12)
chmod -R go+rX "$QA_HOME/app/.venv" "$QA_HOME/python"
sudo ln -sf "$QA_HOME/app/.venv/bin/qa" /usr/local/bin/qa

step "SSH key the runner uses to reach its VMs"
[ -f "$QA_HOME/ssh/id_ed25519" ] || ssh-keygen -q -t ed25519 -N "" -C "espotek-qa-vms" -f "$QA_HOME/ssh/id_ed25519"

step "Host config ($QA_HOME/config.toml)"
if [ ! -f "$QA_HOME/config.toml" ]; then
  winpw=$(rand 20)
  sed "s/{{WINDOWS_PASSWORD}}/$winpw/" "$REPO_DIR/config.example.toml" > "$QA_HOME/config.toml"
  chmod 600 "$QA_HOME/config.toml"
  echo "wrote a new config.toml"
fi

step "Unprivileged account '$WEB_USER' for the public-facing results site and tunnel"
if ! id "$WEB_USER" >/dev/null 2>&1; then
  sudo sysadminctl -addUser "$WEB_USER" -fullName "Labrador QA web" -shell /usr/bin/false \
    -password "$(rand 32)" 2>&1 | grep -v -i "secure token" || true
  id "$WEB_USER" >/dev/null || die "could not create $WEB_USER"
  sudo dscl . create "/Users/$WEB_USER" IsHidden 1
fi
WEB_HOME=$(dscl . -read "/Users/$WEB_USER" NFSHomeDirectory | awk '{print $2}')
sudo mkdir -p "$WEB_HOME/.config/espotek-qa" "$WEB_HOME/.cloudflared" "$WEB_HOME/Library/Logs"
sudo chown -R "$WEB_USER":staff "$WEB_HOME/.config" "$WEB_HOME/.cloudflared" "$WEB_HOME/Library"
sudo chmod 700 "$WEB_HOME/.config/espotek-qa" "$WEB_HOME/.cloudflared"

step "Results site password"
if [ ! -f "$QA_HOME/secrets/web-password" ]; then
  rand 24 > "$QA_HOME/secrets/web-password"
  chmod 600 "$QA_HOME/secrets/web-password"
  NEW_PASSWORD=1
fi
hash=$("$QA_HOME/app/.venv/bin/python" -c \
  'import sys; from werkzeug.security import generate_password_hash; print(generate_password_hash(sys.stdin.read().strip()))' \
  < "$QA_HOME/secrets/web-password")
# Keep the session key across reruns so logins survive a redeploy.
if sudo test -f "$WEB_HOME/.config/espotek-qa/web.env"; then
  secret=$(sudo sed -n 's/^QA_WEB_SECRET_KEY=//p' "$WEB_HOME/.config/espotek-qa/web.env")
else
  secret=$(rand 64)
fi
[ -n "$secret" ] || die "web.env exists but has no QA_WEB_SECRET_KEY"
sudo tee "$WEB_HOME/.config/espotek-qa/web.env" >/dev/null <<EOF
QA_RESULTS_DIR=$QA_HOME/results
QA_WEB_PORT=$WEB_PORT
QA_WEB_SECRET_KEY=$secret
QA_WEB_PASSWORD_HASH=$hash
EOF
sudo chown "$WEB_USER":staff "$WEB_HOME/.config/espotek-qa/web.env"
sudo chmod 600 "$WEB_HOME/.config/espotek-qa/web.env"

step "Cloudflare tunnel for $HOSTNAME_PUBLIC"
if [ -n "$TUNNEL_CREDS" ]; then
  tid=$(sed -n 's/.*"TunnelID":"\([^"]*\)".*/\1/p' "$TUNNEL_CREDS")
  [ -n "$tid" ] || die "$TUNNEL_CREDS has no TunnelID"
  sudo install -o "$WEB_USER" -g staff -m 600 "$TUNNEL_CREDS" "$WEB_HOME/.cloudflared/$tid.json"
fi
creds=$(sudo find "$WEB_HOME/.cloudflared" -name '*.json' | head -1)
[ -n "$creds" ] || die "no tunnel credentials yet; pass --tunnel-credentials <file> (README.md explains how to get one)"
tid=$(basename "$creds" .json)
sudo tee "$WEB_HOME/.cloudflared/config.yml" >/dev/null <<EOF
tunnel: $tid
credentials-file: $creds
no-autoupdate: true
ingress:
  - hostname: $HOSTNAME_PUBLIC
    service: http://127.0.0.1:$WEB_PORT
  - service: http_status:404
EOF
sudo chown "$WEB_USER":staff "$WEB_HOME/.cloudflared/config.yml"

step "Standard account '$MAC_USER' whose desktop the macOS target runs in"
if ! id "$MAC_USER" >/dev/null 2>&1; then
  rand 24 > "$QA_HOME/secrets/$MAC_USER-password"
  chmod 600 "$QA_HOME/secrets/$MAC_USER-password"
  sudo sysadminctl -addUser "$MAC_USER" -fullName "Labrador QA (macOS)" -shell /bin/bash \
    -password "$(cat "$QA_HOME/secrets/$MAC_USER-password")"
  id "$MAC_USER" >/dev/null || die "could not create $MAC_USER"
  sudo createhomedir -c -u "$MAC_USER" >/dev/null
fi
MAC_HOME=$(dscl . -read "/Users/$MAC_USER" NFSHomeDirectory | awk '{print $2}')
# The runner's guest commands are written for bash (as on the Linux guest); zsh
# errors on globs that match nothing.
sudo dscl . -create "/Users/$MAC_USER" UserShell /bin/bash
if dscl . -read /Groups/com.apple.access_ssh >/dev/null 2>&1; then
  sudo dseditgroup -o edit -a "$MAC_USER" -t user com.apple.access_ssh
fi
sudo mkdir -p "$MAC_HOME/.ssh"
sudo cp "$QA_HOME/ssh/id_ed25519.pub" "$MAC_HOME/.ssh/authorized_keys"
sudo chown -R "$MAC_USER":staff "$MAC_HOME/.ssh"
sudo chmod 700 "$MAC_HOME/.ssh"
sudo chmod 600 "$MAC_HOME/.ssh/authorized_keys"
# Its first login would otherwise open Setup Assistant (Apple ID, Siri, Screen Time...) over the desktop.
osver=$(sw_vers -productVersion); osbuild=$(sw_vers -buildVersion)
for key in DidSeeCloudSetup DidSeeSiriSetup DidSeePrivacy DidSeeScreenTime DidSeeAppearanceSetup \
           DidSeeAccessibility DidSeeTouchIDSetup DidSeeApplePaySetup DidSeeAvatarSetup DidSeeiCloudLoginForStorageServices \
           DidSeeActivationLock DidSeeTermsOfAddress DidSeeIntelligence; do
  sudo -u "$MAC_USER" defaults write com.apple.SetupAssistant "$key" -bool true
done
sudo -u "$MAC_USER" defaults write com.apple.SetupAssistant LastSeenCloudProductVersion "$osver"
sudo -u "$MAC_USER" defaults write com.apple.SetupAssistant LastSeenBuddyBuildVersion "$osbuild"
sudo -u "$MAC_USER" defaults write com.apple.SetupAssistant LastPreLoginTasksPerformedVersion "$osver"
sudo -u "$MAC_USER" defaults write com.apple.SetupAssistant LastPreLoginTasksPerformedBuild "$osbuild"
sudo -u "$MAC_USER" defaults -currentHost write com.apple.screensaver idleTime -int 0
# A tester reached over Tailscale must stay reachable when qamac is the one logged
# in: the standalone Tailscale app doesn't run before login, but its tailnet
# state is system-wide (/Library/Tailscale), so starting the app in qamac's
# session brings the same node up.
if [ -d /Applications/Tailscale.app ]; then
  sudo -u "$MAC_USER" defaults write io.tailscale.ipn.macsys AppIntroShown -bool true
  sudo mkdir -p "$MAC_HOME/Library/LaunchAgents"
  sudo tee "$MAC_HOME/Library/LaunchAgents/com.espotek.qa.tailscale.plist" >/dev/null <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.espotek.qa.tailscale</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/open</string><string>-g</string><string>-a</string><string>/Applications/Tailscale.app</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict></plist>
PLIST
  sudo chown -R "$MAC_USER":staff "$MAC_HOME/Library/LaunchAgents"
  sudo chmod 644 "$MAC_HOME/Library/LaunchAgents/com.espotek.qa.tailscale.plist"
fi
# The macOS target needs a desktop session: log this account in automatically at boot.
# (sysadminctl -autologin fails over ssh with SACSetAutoLoginPassword error 22, so
# write the login window's two settings directly: the user, and /etc/kcpassword.)
if [ "$(sudo defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null)" != "$MAC_USER" ] \
   || ! sudo test -f /etc/kcpassword; then
  sudo defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser "$MAC_USER"
  "$QA_HOME/app/.venv/bin/python" -c '
import sys
pw = sys.stdin.read().strip().encode()
key = bytes([0x7D, 0x89, 0x52, 0x23, 0xD2, 0xBC, 0xDD, 0xEA, 0xA3, 0xB9, 0x1F])
data = pw + b"\0"
data += b"\0" * (-len(data) % 12)
sys.stdout.buffer.write(bytes(b ^ key[i % len(key)] for i, b in enumerate(data)))
' < "$QA_HOME/secrets/$MAC_USER-password" | sudo tee /etc/kcpassword >/dev/null
  sudo chown root:wheel /etc/kcpassword
  sudo chmod 600 /etc/kcpassword
  echo "auto-login is now $MAC_USER; reboot before running the macOS target"
fi

step "launchd services"
install_daemon() {  # label, then the plist body via stdin
  local plist="/Library/LaunchDaemons/$1.plist"
  sudo tee "$plist" >/dev/null
  sudo chown root:wheel "$plist"
  sudo chmod 644 "$plist"
  if sudo launchctl print "system/$1" >/dev/null 2>&1; then
    # bootout returns before a KeepAlive job has fully unloaded; bootstrapping
    # too early fails with "5: Input/output error", so wait for it to go.
    sudo launchctl bootout "system/$1"
    for _ in $(seq 1 30); do
      sudo launchctl print "system/$1" >/dev/null 2>&1 || break
      sleep 1
    done
    if sudo launchctl print "system/$1" >/dev/null 2>&1; then die "$1 did not unload"; fi
  fi
  sudo launchctl bootstrap system "$plist"
}
install_daemon com.espotek.qa.web <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.espotek.qa.web</string>
  <key>UserName</key><string>$WEB_USER</string>
  <key>ProgramArguments</key><array>
    <string>$QA_HOME/app/.venv/bin/qa</string><string>web</string>
    <string>--env-file</string><string>$WEB_HOME/.config/espotek-qa/web.env</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>$WEB_HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WEB_HOME/Library/Logs/espotek-qa-web.log</string>
  <key>StandardErrorPath</key><string>$WEB_HOME/Library/Logs/espotek-qa-web.log</string>
</dict></plist>
EOF
install_daemon com.espotek.qa.tunnel <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.espotek.qa.tunnel</string>
  <key>UserName</key><string>$WEB_USER</string>
  <key>ProgramArguments</key><array>
    <string>/opt/local/bin/cloudflared</string><string>--config</string><string>$WEB_HOME/.cloudflared/config.yml</string>
    <string>tunnel</string><string>run</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>$WEB_HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$WEB_HOME/Library/Logs/espotek-qa-tunnel.log</string>
  <key>StandardErrorPath</key><string>$WEB_HOME/Library/Logs/espotek-qa-tunnel.log</string>
</dict></plist>
EOF
install_daemon com.espotek.qa.poll <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.espotek.qa.poll</string>
  <key>UserName</key><string>$ADMIN_USER</string>
  <key>ProgramArguments</key><array><string>/usr/local/bin/qa</string><string>poll</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>$HOME</string>
    <key>PATH</key><string>/usr/local/bin:/opt/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>$QA_HOME/logs/poll.log</string>
  <key>StandardErrorPath</key><string>$QA_HOME/logs/poll.log</string>
</dict></plist>
EOF

step "Checking the results site"
for i in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$WEB_PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$WEB_PORT/healthz" >/dev/null || die "results site isn't answering on 127.0.0.1:$WEB_PORT; see $WEB_HOME/Library/Logs/espotek-qa-web.log"
echo "results site up on 127.0.0.1:$WEB_PORT, public at https://$HOSTNAME_PUBLIC"

if [ "$SKIP_VMS" = 0 ]; then
  for p in $(/usr/local/bin/qa platforms --images); do
    if [ ! -f "$QA_HOME/vms/$p/base.qcow2" ]; then
      step "Golden image for $p (long: OS install and provisioning)"
      /usr/local/bin/qa vm build "$p"
    fi
  done
fi

step "Done"
if [ "${NEW_PASSWORD:-0}" = 1 ]; then
  echo "Results site password (also in $QA_HOME/secrets/web-password): $(cat "$QA_HOME/secrets/web-password")"
else
  echo "Results site password is in $QA_HOME/secrets/web-password"
fi
