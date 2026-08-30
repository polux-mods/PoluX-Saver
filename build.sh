#!/usr/bin/env bash
set -euo pipefail

echo "==> Installing local Node.js runtime..."
NODE_VERSION="${NODE_VERSION:-22.20.0}"
mkdir -p .node
curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz
rm -rf /tmp/node-runtime
mkdir -p /tmp/node-runtime
tar -xJf /tmp/node.tar.xz -C /tmp/node-runtime
rm -rf .node/*
cp -a "/tmp/node-runtime/node-v${NODE_VERSION}-linux-x64/." .node/
export PATH="$PWD/.node/bin:$PATH"

echo "==> Node version:"
node --version

echo "==> Installing Python dependencies..."
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "==> Building BgUtils PO-token provider..."
rm -rf bgutil-ytdlp-pot-provider
git clone --depth 1 --branch 1.3.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil-ytdlp-pot-provider

cd bgutil-ytdlp-pot-provider/server
npm ci
npx tsc
cd ../..

BGUTIL_SCRIPT="$PWD/bgutil-ytdlp-pot-provider/server/build/generate_once.js"
if [ ! -f "$BGUTIL_SCRIPT" ]; then
  echo "ERROR: BgUtils script was not built: $BGUTIL_SCRIPT"
  exit 1
fi

echo "==> BgUtils script ready: $BGUTIL_SCRIPT"
echo "==> Build complete."
