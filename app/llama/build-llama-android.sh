#!/usr/bin/env bash
# Builds llama.cpp's llama-server for Android (arm64) so Stella can think on the phone itself (offline brain).
# Run by GitHub Actions before the APK is built (see .github/workflows/build.yml). Output goes into
# app/src/main/jniLibs/arm64-v8a/, which Android unpacks next to the app on install, where it may be run:
#   libllama_server.so      the llama-server program (renamed: Android only unpacks files named lib*.so)
#   libllama.so, libggml*.so, libmtmd.so, ...   its libraries
#   libggml-cpu-android_*.so   one CPU version per processor generation; the fastest the phone supports is picked
#   libc++_shared.so        the C++ runtime they share
#
# Usage: app/llama/build-llama-android.sh [llama.cpp tag]
set -euo pipefail

TAG="${1:-$(cat "$(dirname "$0")/LLAMA_VERSION")}"
NDK="${ANDROID_NDK_LATEST_HOME:-${ANDROID_NDK_HOME:-${ANDROID_NDK:-}}}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${LLAMA_OUT:-$ROOT/app/src/main/jniLibs/arm64-v8a}"
WORK="${LLAMA_WORK:-$ROOT/.llama-build}"

if [ -z "$NDK" ] || [ ! -d "$NDK" ]; then
  echo "::error::Android NDK not found (ANDROID_NDK_LATEST_HOME / ANDROID_NDK_HOME)." >&2
  exit 1
fi
echo "llama.cpp $TAG, NDK $NDK"

mkdir -p "$WORK"
if [ ! -d "$WORK/src/.git" ]; then
  rm -rf "$WORK/src"
  git clone --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp "$WORK/src"
fi

# Portable build: no global -march. Every processor generation gets its own CPU library and llama.cpp
# loads the best one the phone supports at start-up (older phones still work, newer ones run faster).
cmake -S "$WORK/src" -B "$WORK/build" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
  -DANDROID_ABI=arm64-v8a \
  -DANDROID_PLATFORM=android-28 \
  -DANDROID_STL=c++_shared \
  -DCMAKE_PLATFORM_NO_VERSIONED_SONAME=ON \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=ON \
  -DGGML_BACKEND_DL=ON \
  -DGGML_CPU_ALL_VARIANTS=ON \
  -DGGML_NATIVE=OFF \
  -DGGML_OPENMP=OFF \
  -DGGML_LLAMAFILE=OFF \
  -DLLAMA_OPENSSL=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_APP=OFF \
  -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF

# Only what the server needs: the server itself and every CPU library.
cpu_targets=$(cmake --build "$WORK/build" --target help | sed -nE 's/^(ggml-cpu-android[^:]*): .*/\1/p' | grep -v -e '-feats$' -e '\.so$' | sort -u | tr '\n' ' ')
[ -n "$cpu_targets" ] || { echo "::error::No Android CPU libraries found in the build" >&2; exit 1; }
echo "CPU libraries: $cpu_targets"
cmake --build "$WORK/build" --target llama-server $cpu_targets -j "$(nproc)"

rm -rf "$OUT"
mkdir -p "$OUT"
cp "$WORK/build/bin/llama-server" "$OUT/libllama_server.so"
find "$WORK/build" -name 'lib*.so' -type f -exec cp {} "$OUT/" \;
cp "$NDK/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib/aarch64-linux-android/libc++_shared.so" "$OUT/"
"$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip" --strip-unneeded "$OUT"/*.so || true

# Every library the server asks for must be here, or it won't start on the phone.
READELF="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-readelf"
missing=0
for f in "$OUT"/*.so; do
  for need in $("$READELF" -d "$f" | grep -oE 'Shared library: \[[^]]+\]' | sed -E 's/.*\[(.*)\]/\1/'); do
    case "$need" in
      libc.so|libm.so|libdl.so|liblog.so|libandroid.so) ;;
      *) [ -f "$OUT/$need" ] || { echo "::error::$(basename "$f") needs $need, which wasn't built"; missing=1; } ;;
    esac
  done
done
[ "$missing" = 0 ] || exit 1
ls -la "$OUT"
