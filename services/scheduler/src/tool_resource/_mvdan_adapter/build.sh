#!/bin/sh
set -eu

go_version=1.26.1
parser_version=v3.13.1
adapter_protocol_version=3
archive_sha256=031f088e5d955bab8657ede27ad4e3bc5b7c1ba281f05f245bcc304f327c987a

adapter_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cache_root=${XDG_CACHE_HOME:-"${HOME}/.cache"}/agent-sched-bench
toolchain_dir=$cache_root/go-toolchain-$go_version
binary=$cache_root/mvdan-clause-adapter-protocol-$adapter_protocol_version-mvdan-$parser_version
go_bin=

if command -v go >/dev/null 2>&1 &&
    [ "$(go version | awk '{print $3}')" = "go$go_version" ]; then
    go_bin=$(command -v go)
elif [ -x "$toolchain_dir/bin/go" ] &&
    [ "$("$toolchain_dir/bin/go" version | awk '{print $3}')" = "go$go_version" ]; then
    go_bin=$toolchain_dir/bin/go
else
    case "$(uname -s):$(uname -m)" in
        Linux:x86_64) ;;
        *)
            echo "Go $go_version is required on PATH for this platform." >&2
            exit 1
            ;;
    esac
    mkdir -p "$cache_root"
    download_dir=$(mktemp -d "$cache_root/go-download.XXXXXX")
    trap 'rm -rf "$download_dir"' EXIT HUP INT TERM
    archive=$download_dir/go.tar.gz
    curl -fL "https://go.dev/dl/go$go_version.linux-amd64.tar.gz" -o "$archive"
    printf '%s  %s\n' "$archive_sha256" "$archive" | sha256sum -c -
    tar -C "$download_dir" -xzf "$archive"
    if [ -e "$toolchain_dir" ]; then
        echo "Unexpected existing toolchain at $toolchain_dir; remove it and retry." >&2
        exit 1
    fi
    mv "$download_dir/go" "$toolchain_dir"
    rm -rf "$download_dir"
    trap - EXIT HUP INT TERM
    go_bin=$toolchain_dir/bin/go
fi

mkdir -p "$cache_root/go-mod-cache"
temporary_binary=$binary.tmp.$$
trap 'rm -f "$temporary_binary"' EXIT HUP INT TERM
(
    cd "$adapter_root"
    GOMODCACHE="$cache_root/go-mod-cache" \
        "$go_bin" build -buildvcs=false -trimpath -o "$temporary_binary" .
)
mv "$temporary_binary" "$binary"
printf 'Built %s with Go %s\n' "$binary" "$go_version"
