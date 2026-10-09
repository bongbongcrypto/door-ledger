#!/usr/bin/env bash
# Deploy reporter1 to the server. Run from a developer machine (Linux, macOS or Git Bash):
#
#   bash deploy/deploy.sh
#
# What it does:
#   1. refuses if writer/, tools/ or the unit file have uncommitted changes
#      (the server gets HEAD via git archive, never the working tree)
#   2. uploads the committed writer/ and tools/ to <base>/app over ssh
#   3. on the server: checks the key file is present (never reads it), creates the venv
#      if missing, installs writer/requirements.txt when it changed, swaps the code in
#      (the previous copy stays as writer.prev / tools.prev), writes DOORLEDGER_CODE=<sha>
#      to a systemd drop-in, installs the unit, daemon-reload, enable, restart
#
# Settings (environment): DEPLOY_HOST (required: ssh host alias of the server; its user needs
# passwordless sudo), DEPLOY_PYTHON (python used to create the venv, default python3.11).
set -euo pipefail

# Git Bash would otherwise rewrite /home/... arguments into Windows paths.
export MSYS_NO_PATHCONV=1

HOST="${DEPLOY_HOST:?set DEPLOY_HOST to the ssh host alias of the server}"
PY="${DEPLOY_PYTHON:-python3.11}"
BASE=/home/ubuntu/bots/door-ledger
UNIT=door-ledger.service

cd "$(git rev-parse --show-toplevel)"

dirty="$(git status --porcelain -- writer tools deploy/door-ledger.service)"
if [ -n "$dirty" ]; then
  echo "refusing to deploy: uncommitted changes in writer/, tools/ or deploy/door-ledger.service:" >&2
  printf '%s\n' "$dirty" >&2
  exit 1
fi

CODE="$(git rev-parse --short=12 HEAD)"
if [ -z "$(git branch -r --contains HEAD 2>/dev/null)" ]; then
  echo "warning: $CODE is not on any remote branch; push it so the recorded code version resolves publicly" >&2
fi
echo "deploying $CODE to $HOST:$BASE/app"

# 1. Upload the committed files to a staging directory next to app/.
git archive --format=tar HEAD writer tools deploy/door-ledger.service |
  ssh "$HOST" "set -e; rm -rf $BASE/incoming; mkdir -p $BASE/incoming; tar -xf - -C $BASE/incoming"

# 2. Install on the server. The heredoc is quoted: nothing in it expands locally.
ssh "$HOST" bash -s -- "$CODE" "$BASE" "$UNIT" "$PY" <<'REMOTE'
set -euo pipefail
code="$1"; base="$2"; unit="$3"; py="$4"
app="$base/app"; stage="$base/incoming"; venv="$base/venv"

# The key path comes from the unit itself, so the two cannot drift apart.
key="$(sed -n 's/^Environment=ARKIV_KEY_FILE=//p' "$stage/deploy/$unit")"
if [ -z "$key" ] || [ ! -r "$key" ]; then
  echo "missing key file '$key' on the server: place it (one line ARKIV_SIGNER_HEX=0x..., mode 600) and rerun" >&2
  exit 1
fi
mode="$(stat -c %a "$key")"
if [ "$mode" != 600 ] && [ "$mode" != 400 ]; then
  echo "key file $key has mode $mode: run chmod 600 on it and rerun" >&2
  exit 1
fi

# Python environment: created once, reinstalled only when requirements.txt changes.
if [ ! -x "$venv/bin/python" ]; then
  "$py" -m venv "$venv"
fi
req="$stage/writer/requirements.txt"
if ! cmp -s "$req" "$venv/door-ledger-requirements.txt"; then
  "$venv/bin/python" -m pip install --disable-pip-version-check --only-binary=:all: -r "$req"
  cp "$req" "$venv/door-ledger-requirements.txt"
fi

# Swap the code in. app/state (the reporter's local state) is never touched.
mkdir -p "$app/state"
for d in writer tools; do
  rm -rf "$app/$d.prev"
  if [ -d "$app/$d" ]; then mv "$app/$d" "$app/$d.prev"; fi
  mv "$stage/$d" "$app/$d"
done

# Unit plus a drop-in carrying the deployed code version.
sudo cp "$stage/deploy/$unit" "/etc/systemd/system/$unit"
sudo mkdir -p "/etc/systemd/system/$unit.d"
{ echo "[Service]"; echo "Environment=DOORLEDGER_CODE=$code"; } | sudo tee "/etc/systemd/system/$unit.d/code.conf" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --quiet "$unit"
sudo systemctl restart "$unit"
rm -rf "$stage"

sleep 10
sudo journalctl -u "$unit" -n 15 --no-pager
if systemctl is-active --quiet "$unit"; then
  echo "$unit active, code $code"
else
  echo "$unit is not active: sudo journalctl -u $unit -n 50" >&2
  exit 1
fi
REMOTE
