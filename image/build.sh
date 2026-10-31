#!/bin/bash
# Build the image: image/build.sh [SDK]   SDK: an alia-sdk wheel, or a directory with its
# pyproject.toml (a checkout of alia-devkit's python/), default: the pinned release.
set -euo pipefail
cd "$(dirname "$0")/.."
. image/versions.txt
rm -rf image/wheels && mkdir -p image/wheels
SDK=${1:-https://github.com/NU-Hachimi/alia-devkit/releases/download/sdk-v$ALIA_SDK/alia_sdk-$ALIA_SDK-py3-none-any.whl}
case $SDK in
    http*) curl -sSfL -o image/wheels/$(basename "$SDK") "$SDK" ;;
    *.whl) cp "$SDK" image/wheels/ ;;
    *) python3 -m pip wheel -q --no-deps -w image/wheels "$SDK" ;;
esac
docker build -f image/Dockerfile -t ${IMAGE:-alia-runtime:dev} .
