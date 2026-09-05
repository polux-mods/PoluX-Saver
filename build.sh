#!/usr/bin/env bash
set -euo pipefail

echo "==> Installing local Node.js runtime..."
NODE_VERSION="${NODE_VERSION:-22.20.0}"
mkdir -p .node
curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" -o /tmp/node.tar.xz
tar -xJf /tmp/node.tar.xz -C /tmp
rm -rf .node/*
cp -a "/tmp/node-v${NODE_VERSION}-linux-x64/." .node/
export PATH="$PWD/.node/bin:$PATH"

echo "==> Installing Python dependencies..."
python -m pip install -r requirements.txt

echo "==> Downloading BgUtils PO-token provider source..."
rm -rf bgutil-ytdlp-pot-provider
mkdir -p bgutil-ytdlp-pot-provider

TAR_URL=""
for ref in "refs/heads/master" "refs/heads/main" "refs/tags/v1.3.1" "refs/tags/1.3.1"; do
  if curl --output /dev/null --silent --head --fail "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/${ref}.tar.gz"; then
    TAR_URL="https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/${ref}.tar.gz"
    echo "==> Found archive at: ${ref}"
    break
  fi
done

if [ -z "$TAR_URL" ]; then
  echo "❌ Error: Could not locate source archive on GitHub."
  exit 1
fi

curl -fsSL "$TAR_URL" | tar -xz --strip-components=1 -C bgutil-ytdlp-pot-provider

cd bgutil-ytdlp-pot-provider/server
npm ci
npx tsc
cd ../..

test -f bgutil-ytdlp-pot-provider/server/build/main.js
test -f .node/bin/node

echo "==> BgUtils server: bgutil-ytdlp-pot-provider/server/build/main.js"
echo "==> Node: $(.node/bin/node --version)"
echo "==> Build complete."
