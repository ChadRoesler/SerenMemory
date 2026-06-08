#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  seren-memory-setup.sh  -  one-shot SerenMemory installer (Linux)
#
#  Rip it and win. This script:
#    1. Finds a usable Python (3.10-3.12)
#    2. Makes a clean venv at ~/seren-venvs/memory  (no pip wrestling)
#    3. Installs seren-memory[mcp] from the latest GitHub release (or a
#       local .whl you hand it)
#    4. Writes a friendly config at ~/seren-memory/seren-memory.yaml
#    5. Drops a double-clickable run-seren-memory.sh launcher
#    6. (optional) installs a systemd service so it starts on boot
#
#  The defaults are SAFE: binds 127.0.0.1 (this machine only), no auth.
#  Crank the flags if you want it on the network or behind a token.
#
#  USAGE
#    bash seren-memory-setup.sh                 # easy mode, local-only
#    bash seren-memory-setup.sh --gen-token     # generate a bearer token
#    bash seren-memory-setup.sh --service       # + systemd autostart (sudo)
#    bash seren-memory-setup.sh --wheel ./seren_memory-0.1.0-py3-none-any.whl
#    bash seren-memory-setup.sh --ref v0.4.0    # pin to a release tag
#    bash seren-memory-setup.sh --host 0.0.0.0  # expose on the LAN (careful!)
#
#  FLAGS
#    --port N         Port to listen on            (default 7420)
#    --host HOST      Bind address                 (default 127.0.0.1)
#    --token TOKEN    Set a bearer token
#    --gen-token      Generate a random bearer token for you
#    --wheel PATH     Install from a local .whl instead of GitHub
#    --ref TAG        Pin to a GitHub release tag   (default: latest)
#    --repo SLUG      GitHub repo                   (default ChadRoesler/SerenMemory)
#    --service        Install + enable a systemd unit (needs sudo)
#    --venv PATH      Override venv location        (default ~/seren-venvs/memory)
#    -h, --help       This help
# ══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── OS detection ───────────────────────────────────────────────────────────
OS="$(uname -s)"
IS_MAC=false
[[ "$OS" == "Darwin" ]] && IS_MAC=true

# ── pretty output ──────────────────────────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${B}==>${NC} $1"; }
ok()   { echo -e "${G}  ✓${NC} $1"; }
warn() { echo -e "${Y}  !${NC} $1"; }
die()  { echo -e "${R}ERROR:${NC} $1" >&2; exit 1; }

# ── defaults ───────────────────────────────────────────────────────────────
PORT=7420
HOST="127.0.0.1"          # this machine only. Safe by default.
TOKEN=""
GEN_TOKEN=false
WHEEL=""
REF=""
REPO="ChadRoesler/SerenMemory"
INSTALL_SERVICE=false
VENV_DIR="$HOME/seren-venvs/memory"
APP_DIR="$HOME/seren-memory"
CFG_PATH="$APP_DIR/seren-memory.yaml"

# ── flag parsing (while/case) ──────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)      PORT="$2"; shift 2 ;;
    --host)      HOST="$2"; shift 2 ;;
    --token)     TOKEN="$2"; shift 2 ;;
    --gen-token) GEN_TOKEN=true; shift ;;
    --wheel)     WHEEL="$2"; shift 2 ;;
    --ref)       REF="$2"; shift 2 ;;
    --repo)      REPO="$2"; shift 2 ;;
    --service)   INSTALL_SERVICE=true; shift ;;
    --venv)      VENV_DIR="$2"; shift 2 ;;
    -h|--help)   sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "unknown flag: $1  (try --help)" ;;
  esac
done

echo -e "${G}══════════════════════════════════════════${NC}"
$IS_MAC && echo -e "${G}  SerenMemory setup (macOS)${NC}" || echo -e "${G}  SerenMemory setup (Linux)${NC}"
echo -e "${G}══════════════════════════════════════════${NC}"

# ── 1. find a usable Python ────────────────────────────────────────────────
# chroma 1.x ships a binary wheel (no compiler needed) but its transitive
# deps don't build on Python 3.13+ yet, and the package needs >=3.10. So we
# want 3.10, 3.11, or 3.12 - 3.11/3.12 are the sweet spot.
step "Finding a usable Python (3.10-3.12)"
PYBIN=""
for cand in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
    case "$ver" in
      3.10|3.11|3.12) PYBIN="$cand"; break ;;
    esac
  fi
done
if [[ -z "$PYBIN" ]]; then
  die "No Python 3.10-3.12 found.
  Install one, e.g.:
    macOS:          brew install python@3.12
    Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv
    Fedora:         sudo dnf install python3.12
    Arch:           sudo pacman -S python
  (Avoid 3.13+ for now - a chromadb dependency can't build there yet.)"
fi
PYVER="$("$PYBIN" -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])')"
ok "Using $PYBIN (Python $PYVER)"

# ── 2. resolve the wheel to install ────────────────────────────────────────
WHEEL_SRC=""          # local path we'll pip-install
CLEANUP_WHEEL=false
if [[ -n "$WHEEL" ]]; then
  [[ -f "$WHEEL" ]] || die "wheel not found: $WHEEL"
  WHEEL_SRC="$WHEEL"
  ok "Installing from local wheel: $(basename "$WHEEL")"
else
  step "Resolving the latest SerenMemory release from GitHub ($REPO)"
  command -v curl >/dev/null 2>&1 || die "curl is required to download from GitHub (sudo apt install curl)"
  api="https://api.github.com/repos/${REPO}/releases/${REF:+tags/$REF}"
  [[ -z "$REF" ]] && api="https://api.github.com/repos/${REPO}/releases/latest"
  json="$(curl -fsSL "$api" 2>/dev/null)" || die "GitHub API request failed ($api). Check the repo/tag and your network."
  # Parse with python (no jq dependency - python is already a hard requirement).
  read -r TAG WHL_URL < <("$PYBIN" - "$json" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
tag = data.get("tag_name", "?")
whl = ""
for a in data.get("assets", []):
    if a.get("name", "").endswith(".whl"):
        whl = a["browser_download_url"]; break
print(tag, whl)
PY
)
  [[ -n "$WHL_URL" && "$WHL_URL" != "None" ]] || die "No .whl asset in release '$TAG'. Pass --wheel to install a local file instead."
  ok "Release $TAG  ($(basename "$WHL_URL"))"
  WHEEL_SRC="$(mktemp /tmp/seren_memory_XXXXXX.whl)"
  CLEANUP_WHEEL=true
  trap '[[ "$CLEANUP_WHEEL" == true ]] && rm -f "$WHEEL_SRC"' EXIT
  curl -fsSL "$WHL_URL" -o "$WHEEL_SRC" || die "download failed"
  ok "Downloaded"
fi

# ── 3. venv + install ──────────────────────────────────────────────────────
step "Creating venv at $VENV_DIR"
if [[ -x "$VENV_DIR/bin/python" ]]; then
  warn "venv already exists - reusing it (will upgrade the package)"
else
  "$PYBIN" -m venv "$VENV_DIR" || die "venv creation failed (need the python3-venv package?)"
  ok "venv created"
fi
VPY="$VENV_DIR/bin/python"

step "Installing seren-memory[mcp]  (this pulls chromadb + the MCP SDK)"
"$VPY" -m pip install -q --upgrade pip
# The [mcp] extra adds the MCP route at /mcp alongside the HTTP API. Quoting
# matters: bracket-extras must be glued to the path with no space.
"$VPY" -m pip install -q --upgrade "${WHEEL_SRC}[mcp]" || die "pip install failed - see output above"
ok "Installed"

# ── 4. sanity check (import + the viewer asset that's bitten us before) ─────
step "Sanity-checking the install"
CHECK="$("$VPY" - <<'PY'
import pathlib
try:
    import seren_memory
except Exception as e:
    print(f"IMPORT_FAILED: {e}"); raise SystemExit
v = pathlib.Path(seren_memory.__file__).parent / "viewer" / "halls.html"
print("OK" if v.exists() else "VIEWER_MISSING")
PY
)"
case "$CHECK" in
  OK) ok "Package imports and the Halls viewer asset is present" ;;
  VIEWER_MISSING) warn "Package installed but halls.html is missing - /viewer will 404 (wheel-packaging regression)" ;;
  *) die "Install looks broken: $CHECK" ;;
esac

# ── 5. config ──────────────────────────────────────────────────────────────
step "Writing config at $CFG_PATH"
mkdir -p "$APP_DIR"
$GEN_TOKEN && TOKEN="$("$VPY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
if [[ -f "$CFG_PATH" ]]; then
  bak="$CFG_PATH.bak.$(date +%s)"
  cp "$CFG_PATH" "$bak"
  warn "Existing config backed up to $(basename "$bak")"
fi
cat > "$CFG_PATH" <<YAML
# SerenMemory config - generated by seren-memory-setup.sh
# Full reference: see seren-memory.yaml.sample in the repo.
server:
  host: ${HOST}          # 127.0.0.1 = this machine only; 0.0.0.0 = the LAN
  port: ${PORT}
  # Empty = no auth (fine for local). A token requires
  #   Authorization: Bearer <token>  on every route except / and /health.
  bearer_token: "${TOKEN}"

storage:
  # ~ expands to your home dir. Created on first run. THIS is your memory -
  # back it up, and it survives package upgrades untouched.
  persist_dir: ~/.seren-memory/chroma
YAML
# If a token is set, the config holds a secret - lock it down.
[[ -n "$TOKEN" ]] && chmod 600 "$CFG_PATH" && ok "Config locked to 0600 (it holds your token)"
ok "Config written"

# ── 6. launcher (the rip-it-and-win artifact) ──────────────────────────────
LAUNCHER="$APP_DIR/run-seren-memory.sh"
cat > "$LAUNCHER" <<SH
#!/usr/bin/env bash
# Start SerenMemory. Just run this (or double-click it).
exec "$VPY" -m seren_memory --config "$CFG_PATH"
SH
chmod +x "$LAUNCHER"
ok "Launcher: $LAUNCHER"

# ── 7. optional autostart ──────────────────────────────────────────────────
if $INSTALL_SERVICE; then
  if $IS_MAC; then
    # ── macOS: launchd user agent (no sudo needed) ──────────────────────────
    step "Installing launchd user agent (starts at login, no sudo needed)"
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST="$PLIST_DIR/com.chadroesler.seren-memory.plist"
    mkdir -p "$PLIST_DIR"
    cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.chadroesler.seren-memory</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VPY</string>
        <string>-m</string>
        <string>seren_memory</string>
        <string>--config</string>
        <string>$CFG_PATH</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$APP_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$APP_DIR/seren-memory.log</string>
    <key>StandardErrorPath</key>
    <string>$APP_DIR/seren-memory.err</string>
</dict>
</plist>
PLISTEOF
    # Unload first in case it was previously registered, then load.
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load -w "$PLIST"
    ok "launchd agent installed: $PLIST"
    ok "Starts automatically at login. Logs: $APP_DIR/seren-memory.log"
    step "Waiting for it to come up"
    for i in $(seq 1 30); do
      sleep 0.5
      if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        ok "SerenMemory is responding"; break
      fi
      [[ $i -eq 30 ]] && warn "Didn't respond in 15s - check: tail -f $APP_DIR/seren-memory.log"
    done
  else
    # ── Linux: systemd system service (needs sudo) ───────────────────────────
    step "Installing systemd service (needs sudo)"
    UNIT=/etc/systemd/system/seren-memory.service
    ENVFILE="$APP_DIR/seren-memory.env"
    # Keep the token out of the unit text (it shows in `systemctl show`).
    if [[ -n "$TOKEN" ]]; then
      printf 'SEREN_MEMORY_BEARER_TOKEN=%s\n' "$TOKEN" > "$ENVFILE"
      chmod 600 "$ENVFILE"
    fi
    sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=SerenMemory - three-tier LLM memory
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$APP_DIR
ExecStart=$VPY -m seren_memory --config $CFG_PATH
$( [[ -n "$TOKEN" ]] && echo "EnvironmentFile=$ENVFILE" )
Restart=on-failure
RestartSec=5
MemoryMax=2G

[Install]
WantedBy=multi-user.target
UNITEOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now seren-memory
    ok "Service installed and started"
    step "Waiting for it to come up"
    for i in $(seq 1 30); do
      sleep 0.5
      if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        ok "SerenMemory is responding"; break
      fi
      [[ $i -eq 30 ]] && warn "Didn't respond in 15s - check: journalctl -u seren-memory -f"
    done
  fi
fi

# ── done ───────────────────────────────────────────────────────────────────
echo
echo -e "${G}══════════════════════════════════════════${NC}"
echo -e "${G}  SerenMemory is set up ✓${NC}"
echo -e "${G}══════════════════════════════════════════${NC}"
if ! $INSTALL_SERVICE; then
  echo -e "  Start it:        ${B}$LAUNCHER${NC}"
fi
echo -e "  Viewer:          ${B}http://${HOST}:${PORT}/viewer${NC}"
echo -e "  MCP endpoint:    ${B}http://${HOST}:${PORT}/mcp/${NC}   (note the trailing slash)"
echo -e "  VSCode plugin:   set serenMemory.endpoint to ${B}http://${HOST}:${PORT}${NC}"
[[ -n "$TOKEN" ]] && echo -e "  Bearer token:    ${Y}${TOKEN}${NC}  (also set it in the plugin via 'Seren Memory: Set Bearer Token')"
echo
warn "First write/search downloads the embedding model (~80MB) - that one needs internet."
echo -e "${G}Rip it and win. 🌭🔧${NC}"