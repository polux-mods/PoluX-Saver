#!/usr/bin/env bash
set -euo pipefail

# Забороняємо Git запитувати логін/пароль в терміналі
export GIT_TERMINAL_PROMPT=0

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

echo "==> Building BgUtils PO-token provider..."
rm -rf bgutil-ytdlp-pot-provider

# Клонуємо основну гілку main без прив'язки до відсутнього тегу
git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil-ytdlp-pot-provider

cd bgutil-ytdlp-pot-provider/server
npm ci
npx tsc
cd ../..

test -f bgutil-ytdlp-pot-provider/server/build/main.js
test -f .node/bin/node

echo "==> BgUtils server: bgutil-ytdlp-pot-provider/server/build/main.js"
echo "==> Node: $(.node/bin/node --version)"
echo "==> Build complete."
