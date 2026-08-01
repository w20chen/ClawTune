#!/bin/sh
set -eu

go_version=1.26.1
parser_version=v3.13.1
adapter_protocol_version=3

# Map uname -m to Go's architecture name.  Only Linux is supported for
# auto-download; on other platforms Go $go_version must be on PATH.
go_arch=""
case "$(uname -s):$(uname -m)" in
    Linux:x86_64)  go_arch="amd64" ;;
    Linux:aarch64) go_arch="arm64" ;;
    Linux:arm64)   go_arch="arm64" ;;
    *)
        echo "Go $go_version is required on PATH for this platform ($(uname -s)-$(uname -m))." >&2
        exit 1
        ;;
esac

adapter_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cache_root=${XDG_CACHE_HOME:-"${HOME}/.cache"}/agent-sched-bench
toolchain_dir=$cache_root/go-toolchain-$go_version-$go_arch
binary=$cache_root/mvdan-clause-adapter-protocol-$adapter_protocol_version-mvdan-$parser_version-linux-$go_arch
go_bin=

if command -v go >/dev/null 2>&1 &&
    [ "$(go version | awk '{print $3}')" = "go$go_version" ]; then
    go_bin=$(command -v go)
elif [ -x "$toolchain_dir/bin/go" ] &&
    [ "$("$toolchain_dir/bin/go" version | awk '{print $3}')" = "go$go_version" ]; then
    go_bin=$toolchain_dir/bin/go
else
    mkdir -p "$cache_root"
    download_dir=$(mktemp -d "$cache_root/go-download.XXXXXX")
    trap 'rm -rf "$download_dir"' EXIT HUP INT TERM
    archive=$download_dir/go.tar.gz

    # Fetch the official SHA256 checksum so we can verify the tarball
    # regardless of which mirror serves it.
    _checksum_url="https://go.dev/dl/go${go_version}.linux-${go_arch}.tar.gz.sha256"
    _checksum_file="$download_dir/go.tar.gz.sha256"
    echo "Fetching checksum: $_checksum_url" >&2
    if ! curl -fsSL --connect-timeout 15 --max-time 30 "$_checksum_url" -o "$_checksum_file"; then
        echo "Failed to fetch checksum; cannot verify Go download integrity." >&2
        exit 1
    fi
    _expected_hash=$(cut -d' ' -f1 "$_checksum_file")
    if [ -z "$_expected_hash" ] || [ ${#_expected_hash} -ne 64 ]; then
        echo "Invalid checksum file from $_checksum_url" >&2
        exit 1
    fi

    # Download the Go tarball from one of several mirrors.
    _go_mirrors="
https://go.dev/dl/
https://golang.google.cn/dl/
https://mirrors.aliyun.com/golang/
"
    _downloaded=0
    for _mirror in $_go_mirrors; do
        _url="${_mirror}go${go_version}.linux-${go_arch}.tar.gz"
        echo "Trying Go download: $_url" >&2
        if curl -fL --connect-timeout 15 --max-time 120 "$_url" -o "$archive" 2>/dev/null; then
            echo "Downloaded from $_mirror" >&2
            _downloaded=1
            break
        fi
        echo "Failed: $_url" >&2
    done
    if [ "$_downloaded" -ne 1 ]; then
        echo "All Go mirrors failed." >&2
        exit 1
    fi

    # Verify the downloaded tarball against the official checksum.
    _actual_hash=$(sha256sum "$archive" | cut -d' ' -f1)
    if [ "$_expected_hash" != "$_actual_hash" ]; then
        echo "SHA256 mismatch for go${go_version}.linux-${go_arch}.tar.gz" >&2
        echo "Expected: $_expected_hash" >&2
        echo "Got:      $_actual_hash" >&2
        exit 1
    fi
    echo "SHA256 verified" >&2

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
        GOPROXY="https://goproxy.cn,https://proxy.golang.org,direct" \
        "$go_bin" build -buildvcs=false -trimpath -o "$temporary_binary" .
)
mv "$temporary_binary" "$binary"
printf 'Built %s with Go %s\n' "$binary" "$go_version"
