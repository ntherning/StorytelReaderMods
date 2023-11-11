#!/bin/bash

set -e 
set -o pipefail

BASE=$(cd "$(dirname "$0")/../.."; pwd -P)
CROSSBUILD_IMAGE="ntherning/crossbuild:latest"

if [ ! -d "${BASE}/.git/" ]; then
  echo "'$BASE' does not appear to be a git repository root"
  exit 1
fi

build_tools() {
    os_arch=$1
    cross_triple=$2
    host_wd=${BASE}/build/rkflashtool/build/${os_arch}
    guest_wd=/host/build/rkflashtool/build/${os_arch}
    toolchain=/host/build/rkflashtool/toolchain-${os_arch}.cmake
    install_prefix=/host/tools/${os_arch}

    mkdir -p "${host_wd}"
    docker run -it --rm -v "${BASE}":/host -w ${guest_wd} -e CROSS_TRIPLE=${cross_triple} ${CROSSBUILD_IMAGE} \
        cmake -DCMAKE_TOOLCHAIN_FILE=${toolchain} /host/build/rkflashtool -DCMAKE_INSTALL_PREFIX=${install_prefix}
    docker run -it --rm -v "${BASE}":/host -w ${guest_wd} -e CROSS_TRIPLE=${cross_triple} ${CROSSBUILD_IMAGE} \
        make install

    mv "${BASE}/tools/${os_arch}/bin"/* "${BASE}/tools/${os_arch}"
    rm -rf "${BASE}/tools/${os_arch}/bin"
}

TARGETS=""

while [ ! -z "$1" ]; do
    case $1 in
        macos-arm64|macos-x86_64|windows-x86_64)
            TARGETS="$TARGETS $1"
            ;;
        macos)
            TARGETS="$TARGETS macos-arm64 macos-x86_64"
            ;;
        windows)
            TARGETS="$TARGETS windows-x86_64"
            ;;
        all)
            TARGETS="$TARGETS macos-arm64 macos-x86_64 windows-x86_64"
            ;;
        *)
            echo "Unrecognized target '$1'"
            exit 1
            ;;
    esac
    shift
done

TARGETS=$(for T in $TARGETS; do echo $T; done | sort | uniq)
for TARGET in $TARGETS; do
    CROSS_TRIPLE=
    case $TARGET in
        macos-arm64) CROSS_TRIPLE=aarch64-apple-darwin ;;
        macos-x86_64) CROSS_TRIPLE=x86_64-apple-darwin ;;
        windows-x86_64) CROSS_TRIPLE=x86_64-w64-mingw32 ;;
    esac
    build_tools $TARGET $CROSS_TRIPLE
done
