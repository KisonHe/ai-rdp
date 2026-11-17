#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
version=3.32.2
commit=97eff6ddfbbfaa8e42a126d362686bd001ff8bdc
if [ ! -d third_party/FreeRDP/.git ]; then
  git clone --depth 1 --branch "$version" https://github.com/FreeRDP/FreeRDP.git third_party/FreeRDP
fi
actual=$(git -C third_party/FreeRDP rev-parse HEAD)
if [ "$actual" != "$commit" ]; then
  echo "Unexpected FreeRDP revision: $actual" >&2
  exit 1
fi
openssl_root=${AI_RDP_OPENSSL_ROOT:-/opt/homebrew/opt/openssl@3}
cmake -S third_party/FreeRDP -B build/freerdp \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$PWD/build/deps" \
  -DOPENSSL_ROOT_DIR="$openssl_root" \
  -DWITH_CLIENT=OFF -DWITH_CLIENT_COMMON=OFF -DWITH_SERVER=OFF \
  -DWITH_CHANNELS=OFF -DWITH_X11=OFF -DWITH_WAYLAND=OFF -DWITH_SAMPLE=OFF \
  -DWITH_FFMPEG=OFF -DWITH_SWSCALE=OFF -DWITH_OPENH264=OFF \
  -DWITH_CAIRO=OFF -DWITH_CUPS=OFF -DWITH_PULSE=OFF -DWITH_ALSA=OFF \
  -DWITH_OPUS=OFF -DWITH_URIPARSER=OFF -DWITH_PCSC=OFF -DWITH_PKCS11=OFF \
  -DWITH_KRB5=OFF -DWITH_FUSE=OFF -DWITH_WINPR_TOOLS=OFF \
  -DWITH_WINPR_JSON=OFF -DWITH_AAD=OFF -DBUILD_TESTING=OFF
cmake --build build/freerdp --parallel 6
cmake --install build/freerdp
PKG_CONFIG_PATH="$PWD/build/deps/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
  cmake -S . -B build/native -DOPENSSL_ROOT_DIR="$openssl_root"
cmake --build build/native --parallel 4
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -r requirements.txt
