#!/usr/bin/env bash
# One-time codespace setup (D31): the pinned testbed tools, the venv, and a D25 git identity.
# The tool versions are read from the Makefile so the codespace never drifts from the pins.
set -euo pipefail

cd "$(dirname "$0")/.."

pin() { sed -n "s/^$1[[:space:]]*?=[[:space:]]*//p" Makefile | head -n1; }
KIND_VERSION="$(pin KIND_VERSION)"
KUBECTL_VERSION="$(pin KUBECTL_VERSION)"
HELM_VERSION="$(pin HELM_VERSION)"
echo "[post-create] kind $KIND_VERSION, kubectl $KUBECTL_VERSION, helm $HELM_VERSION"

ARCH="$(dpkg --print-architecture)" # amd64 / arm64
BIN=/usr/local/bin

sudo curl -fsSLo "$BIN/kind" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${ARCH}"
sudo chmod +x "$BIN/kind"

sudo curl -fsSLo "$BIN/kubectl" "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl"
sudo chmod +x "$BIN/kubectl"

curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" | tar -xz -C /tmp
sudo mv "/tmp/linux-${ARCH}/helm" "$BIN/helm"

# bookworm's python3 is 3.11; venv and headers are separate packages.
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev >/dev/null

python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python3 -m pip install --quiet --upgrade pip
make install

# D25: the author is the GitHub username with its noreply email, never a display name.
if [ -n "${GITHUB_USER:-}" ]; then
  git config --local user.name "$GITHUB_USER"
  git config --local user.email "${GITHUB_USER}@users.noreply.github.com"
fi

# Every `make` in a codespace terminal should see the venv, like on the laptop.
grep -q 'inframind-core/.venv' ~/.bashrc 2>/dev/null \
  || echo '[ -f /workspaces/inframind-core/.venv/bin/activate ] && . /workspaces/inframind-core/.venv/bin/activate' >> ~/.bashrc

# scripts/codespace.sh waits for this marker before running any make target.
touch "$HOME/.inframind-ready"
echo "[post-create] done"
