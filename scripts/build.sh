#!/bin/sh
set -eu

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"
builder=netdesk-builder:3.24.1
command -v docker >/dev/null 2>&1 || { echo 'Docker is required.' >&2; exit 1; }
if [ "$(uname -m)" != x86_64 ]; then
    echo 'NetDesk v1 requires an x86_64 Linux build host.' >&2
    exit 1
fi

mkdir -p dist
docker build --platform linux/amd64 --tag "$builder" .
docker run --rm --platform linux/amd64 \
    --mount "type=bind,src=$repo,dst=/src,readonly" \
    --mount "type=bind,src=$repo/dist,dst=/out" \
    --env "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}" \
    --env "OUTPUT_UID=$(id -u)" --env "OUTPUT_GID=$(id -g)" \
    "$builder" /src/scripts/build-rootfs.sh
