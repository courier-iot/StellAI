#!/usr/bin/env bash
# Builds whisper.cpp's whisper-cli for Android (arm64) so speech can be turned into text on the phone (no server).
# Run by GitHub Actions after build-llama-android.sh. Output goes into app/src/main/jniLibs/arm64-v8a/:
#   libwhisper_cli.so          for any 64-bit ARM phone
#   libwhisper_cli_fast.so     for newer processors (dot-product and half-precision instructions), much faster
# Both are single programs with everything built in, so they can't clash with llama.cpp's libraries.
#
# Usage: app/llama/build-whisper-android.sh [whisper.cpp tag]
set -euo pipefail

TAG="${1:-$(cat "$(dirname "$0")/WHISPER_VERSION")}"
NDK="${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-${ANDROID_NDK:-}}}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${WHISPER_OUT:-$ROOT/app/src/main/jniLibs/arm64-v8a}"
WORK="${WHISPER_WORK:-$ROOT/.whisper-build}"

if [ -z "$NDK" ] || [ ! -d "$NDK" ]; then
  echo "::error::Android NDK not found (ANDROID_NDK_LATEST_HOME / ANDROID_NDK_HOME)." >&2
  exit 1
fi
echo "whisper.cpp $TAG, NDK $NDK"

mkdir -p "$WORK" "$OUT"
if [ ! -d "$WORK/src/.git" ]; then
  rm -rf "$WORK/src"
  git clone --depth 1 --branch "$TAG" https://github.com/ggml-org/whisper.cpp "$WORK/src"
fi

build() {   # build <folder> <output name> [extra cmake args…]
  local dir="$1" name="$2"
  shift 2
  cmake -S "$WORK/src" -B "$WORK/$dir" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI=arm64-v8a \
    -DANDROID_PLATFORM=android-28 \
    -DANDROID_STL=c++_static \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_NATIVE=OFF \
    -DGGML_OPENMP=OFF \
    -DWHISPER_BUILD_TESTS=OFF \
    -DWHISPER_BUILD_SERVER=OFF \
    -DWHISPER_BUILD_EXAMPLES=ON \
    -DWHISPER_SDL2=OFF \
    -DWHISPER_CURL=OFF \
    "$@"
  cmake --build "$WORK/$dir" --target whisper-cli -j "$(nproc)"
  cp "$WORK/$dir/bin/whisper-cli" "$OUT/$name"
  "$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip" --strip-unneeded "$OUT/$name" || true
}

# any 64-bit ARM processor
build build-base libwhisper_cli.so -DGGML_CPU_ARM_ARCH=armv8-a
# newer processors (most phones from 2019 on)
build build-fast libwhisper_cli_fast.so -DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+fp16

# They must not need any library that isn't on every phone
READELF="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"
for f in "$OUT/libwhisper_cli.so" "$OUT/libwhisper_cli_fast.so"; do
  for need in $("$READELF" -d "$f" | grep -oE 'Shared library: \[[^]]+\]' | sed -E 's/.*\[(.*)\]/\1/'); do
    case "$need" in
      libc.so|libm.so|libdl.so|liblog.so|libandroid.so) ;;
      *) echo "::error::$(basename "$f") needs $need"; exit 1 ;;
    esac
  done
done
ls -la "$OUT"/libwhisper_cli*.so
