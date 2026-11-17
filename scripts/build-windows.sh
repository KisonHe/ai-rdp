#!/usr/bin/env bash
# Developer build only. Run in the MSYS2 UCRT64 shell; releases need no compiler.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ ${MSYSTEM:-} == UCRT64 ]] || { echo 'Use the MSYS2 UCRT64 shell' >&2; exit 1; }
version=3.32.2
commit=97eff6ddfbbfaa8e42a126d362686bd001ff8bdc
if [[ ! -d third_party/FreeRDP ]]; then
  git clone --depth 1 --branch "$version" https://github.com/FreeRDP/FreeRDP.git third_party/FreeRDP
fi
if [[ -d third_party/FreeRDP/.git ]]; then
  actual=$(git -C third_party/FreeRDP rev-parse HEAD)
else
  actual=$(cat third_party/FreeRDP/.source-commit)
fi
[[ $actual == "$commit" ]] || { echo 'Unexpected FreeRDP source revision' >&2; exit 1; }
prefix=$(cygpath -m "$PWD/build/deps")
cmake -G Ninja -S third_party/FreeRDP -B build/freerdp-windows \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
  -DCMAKE_C_FLAGS=-D__STDC_NO_THREADS__ \
  -DUSE_UNWIND=OFF -DWITHOUT_FREERDP_3x_DEPRECATED=ON \
  -DWITH_CLIENT=OFF -DWITH_CLIENT_COMMON=OFF -DWITH_SERVER=OFF \
  -DWITH_CHANNELS=OFF -DWITH_X11=OFF -DWITH_WAYLAND=OFF -DWITH_SAMPLE=OFF \
  -DWITH_FFMPEG=OFF -DWITH_SWSCALE=OFF -DWITH_OPENH264=OFF \
  -DWITH_CAIRO=OFF -DWITH_CUPS=OFF -DWITH_PULSE=OFF -DWITH_ALSA=OFF \
  -DWITH_OPUS=OFF -DWITH_URIPARSER=OFF -DWITH_PCSC=OFF -DWITH_PKCS11=OFF \
  -DWITH_KRB5=OFF -DWITH_FUSE=OFF -DWITH_WINPR_TOOLS=OFF \
  -DWITH_WINPR_JSON=OFF -DWITH_AAD=OFF -DBUILD_TESTING=OFF
cmake --build build/freerdp-windows --parallel "${AI_RDP_BUILD_JOBS:-6}"
cmake --install build/freerdp-windows
export PKG_CONFIG_PATH="$PWD/build/deps/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
cmake -G Ninja -S . -B build/native -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --parallel 4
