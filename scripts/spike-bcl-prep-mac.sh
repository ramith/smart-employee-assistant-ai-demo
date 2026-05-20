#!/usr/bin/env bash
#
# spike-bcl-prep-mac.sh — first-time macOS setup for the C12 BCL spike.
#
# What it does:
#   1. Verifies Homebrew is installed.
#   2. Installs autossh (used by spike-bcl-up.sh to maintain the reverse
#      tunnel across laptop sleep/wake).
#   3. Verifies Docker Desktop is running.
#   4. Prompts for AWS_VM_HOST + AWS_VM_USER and writes them to .env.
#   5. Tests SSH connectivity (BatchMode, ConnectTimeout 5 s) using your
#      existing SSH key — does NOT create new credentials.
#   6. Pre-pulls python:3.11-slim so `up` is fast.
#   7. Creates tools/_bcl_log/ for the listener's capture log.
#
# Idempotent — safe to re-run. Aborts on the first hard failure.
#
# Run from the repo root or scripts/ — the script cd's to the repo root.

set -euo pipefail

cd "$(dirname "$0")/.."

# ── Colourised output ────────────────────────────────────────────────────────
_R="\033[0m"; _B="\033[1m"; _G="\033[32m"; _Y="\033[33m"; _RD="\033[31m"
ok()    { printf "  ${_G}✓${_R} %s\n" "$1"; }
info()  { printf "  ${_B}→${_R} %s\n" "$1"; }
warn()  { printf "  ${_Y}!${_R} %s\n" "$1"; }
fail()  { printf "  ${_RD}✗${_R} %s\n" "$1"; }
hdr()   { printf "\n${_B}── %s ──${_R}\n" "$1"; }

hdr "C12 BCL spike — macOS first-time setup"
echo "  This script prepares your laptop to run the BCL capture rig that"
echo "  receives back-channel logout POSTs from WSO2 IS on the AWS VM."
echo "  Existing SSH key + AWS access are required; no new credentials made."

# ── 1. Homebrew ──────────────────────────────────────────────────────────────
hdr "1/7 — Homebrew"
if ! command -v brew >/dev/null 2>&1; then
  fail "Homebrew not found."
  echo "    Install from https://brew.sh and re-run this script."
  exit 1
fi
ok "Homebrew installed"

# ── 2. autossh ────────────────────────────────────────────────────────────────
hdr "2/7 — autossh"
if ! command -v autossh >/dev/null 2>&1; then
  info "installing autossh via Homebrew"
  brew install autossh
fi
ok "autossh installed ($(autossh -V 2>&1 | head -1))"

# ── 3. Docker Desktop ────────────────────────────────────────────────────────
hdr "3/7 — Docker Desktop"
if ! command -v docker >/dev/null 2>&1; then
  fail "Docker CLI not found. Install Docker Desktop for Mac."
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  fail "Docker daemon is not running. Start Docker Desktop and re-run."
  exit 1
fi
ok "Docker is running"

# ── 4. Repo-root .env: AWS_VM_HOST + AWS_VM_USER ─────────────────────────────
hdr "4/7 — AWS VM identity (.env)"
ENV_FILE=".env"
touch "$ENV_FILE"

read_or_keep() {
  local key="$1" prompt="$2" allow_empty="${3:-no}" current
  current="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  if [[ -n "$current" ]]; then
    info "$key already set to '$current' (keeping)"
    return
  fi
  read -r -p "  $prompt: " value
  if [[ -z "$value" ]] && [[ "$allow_empty" != "yes" ]]; then
    fail "$key cannot be empty"
    exit 1
  fi
  printf "%s=%s\n" "$key" "$value" >> "$ENV_FILE"
  ok "$key written to $ENV_FILE"
}

read_or_keep "AWS_VM_HOST" "AWS VM host or IP (e.g. 13.60.190.47)"
read_or_keep "AWS_VM_USER" "AWS VM SSH user (e.g. ec2-user, ubuntu)"
read_or_keep "AWS_VM_KEY"  "Path to SSH private key (.pem). Leave blank if loaded into ssh-agent or ~/.ssh/config" "yes"

# Reload — `set -a` exports every key during the source.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# ── 5. SSH connectivity test ─────────────────────────────────────────────────
hdr "5/7 — SSH connectivity to ${AWS_VM_USER}@${AWS_VM_HOST}"

# Build SSH options — pass -i only if the user gave a key path.
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new)
if [[ -n "${AWS_VM_KEY:-}" ]]; then
  if [[ ! -f "${AWS_VM_KEY/#~/$HOME}" ]]; then
    fail "AWS_VM_KEY points to a missing file: ${AWS_VM_KEY}"
    exit 1
  fi
  # SSH refuses to use a key whose permissions are too open.
  KEY_PATH="${AWS_VM_KEY/#~/$HOME}"
  KEY_PERM="$(stat -f "%OLp" "$KEY_PATH" 2>/dev/null || stat -c "%a" "$KEY_PATH" 2>/dev/null || echo "?")"
  if [[ "$KEY_PERM" != "600" ]] && [[ "$KEY_PERM" != "400" ]]; then
    info "tightening permissions on $KEY_PATH (was $KEY_PERM → 600)"
    chmod 600 "$KEY_PATH"
  fi
  SSH_OPTS+=(-i "$KEY_PATH")
  info "using key: $KEY_PATH"
else
  info "no AWS_VM_KEY set — relying on ssh-agent / ~/.ssh/config"
fi

if ssh "${SSH_OPTS[@]}" "${AWS_VM_USER}@${AWS_VM_HOST}" "echo ok" 2>/dev/null | grep -q "^ok$"; then
  ok "SSH login OK"
else
  fail "SSH connection failed in BatchMode."
  echo "    Try manually:"
  if [[ -n "${AWS_VM_KEY:-}" ]]; then
    echo "      ssh -i ${AWS_VM_KEY} ${AWS_VM_USER}@${AWS_VM_HOST}"
  else
    echo "      ssh ${AWS_VM_USER}@${AWS_VM_HOST}"
  fi
  echo "    Common causes:"
  echo "      • Wrong key path (check AWS_VM_KEY in .env)"
  echo "      • Key not authorized on the VM (ssh-copy-id or AWS Console paste)"
  echo "      • Security group blocks port 22 from your IP"
  echo "      • Passphrase-protected key not loaded into ssh-agent"
  exit 1
fi

# ── 6. Pre-pull listener image ───────────────────────────────────────────────
hdr "6/7 — Pre-pull python:3.11-slim"
docker pull python:3.11-slim >/dev/null 2>&1 \
  && ok "python:3.11-slim ready" \
  || warn "could not pre-pull (continuing — first up may be slower)"

# ── 7. Capture log directory ─────────────────────────────────────────────────
hdr "7/7 — Capture log directory"
mkdir -p tools/_bcl_log
ok "tools/_bcl_log/ ready"

# ── Done ─────────────────────────────────────────────────────────────────────
hdr "Setup complete"
cat <<'EOF'

  Next step:
      ./scripts/spike-bcl-up.sh

  That will:
    • start the bcl-listener container (bound to 127.0.0.1:8123)
    • bring up an autossh reverse tunnel: AWS-VM:localhost:8123 → laptop:8123
    • print the BCL URL to register in WSO2 IS Console

  Tear down with:
      ./scripts/spike-bcl-down.sh
EOF
