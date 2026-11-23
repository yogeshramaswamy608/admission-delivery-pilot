#!/usr/bin/env bash
# Reproduce the Ubuntu x86-64 source server without system installation.
set -euo pipefail
runtime_dir=${1:-/tmp/h2-candle-runtime}
package_hash=55ac4cc583c229664f5a528d5771cf75d24aaf5e7a70ec7a20413084b8091317
dotnet_hash=ecf5b14d8a7ec32a3662f131e74c40f9003534e98b24c682c07c0d68f653d08fb1d67c9b3497efdd3d2790179cbeda9fc80b1f88736964411719d94feada9533
icu_hash=c8b97930f9e365d6d00978144b468ac8397ef07d2fb2c453869f05fc3a98c4ca
icu_deb=libicu78_78.2-2ubuntu1_amd64.deb
mkdir -p "$runtime_dir"
cd "$runtime_dir"
# Always verify an existing download before reusing it. A failed fetch writes a separate partial file.
fetch() {
    local url=$1 target=$2 algorithm=$3 expected=$4
    if [[ ! -f $target ]]; then
        local partial
        partial=$(mktemp "./$target.partial.XXXXXX")
        curl --fail --location --silent --show-error --max-time 180 "$url" --output "$partial"
        printf '%s  %s\n' "$expected" "$partial" | "$algorithm" --check --status
        mv --no-clobber "$partial" "$target"
    fi
    printf '%s  %s\n' "$expected" "$target" | "$algorithm" --check --status
}
fetch https://api.nuget.org/v3-flatcontainer/fhir-candle/2026.708.1348/fhir-candle.2026.708.1348.nupkg candle.nupkg sha256sum "$package_hash"
fetch https://builds.dotnet.microsoft.com/dotnet/aspnetcore/Runtime/8.0.34/aspnetcore-runtime-8.0.34-linux-x64.tar.gz aspnet.tar.gz sha512sum "$dotnet_hash"
fetch "https://archive.ubuntu.com/ubuntu/pool/main/i/icu/$icu_deb" "$icu_deb" sha256sum "$icu_hash"
if [[ ! -x dotnet/dotnet ]]; then
    mkdir -p dotnet
    tar -xzf aspnet.tar.gz -C dotnet
fi
if [[ ! -e icu/usr/lib/x86_64-linux-gnu/libicuuc.so.78 ]]; then
    dpkg-deb -x "$icu_deb" icu
fi
python3 - <<'PY'
from pathlib import Path
from zipfile import ZipFile
with ZipFile('candle.nupkg') as archive:
    names = [n for n in archive.namelist() if n.startswith('tools/net8.0/any/') and not n.endswith('/')]
    for name in names:
        path = Path('candle') / name
        if not path.exists():
            archive.extract(name,'candle')
        if path.read_bytes() != archive.read(name):
            raise SystemExit(f'Installed file differs from the pinned package: {path}')
    print(f'Pinned package verified, including {len(names)} .NET 8 files.')
PY
LD_LIBRARY_PATH="$PWD/icu/usr/lib/x86_64-linux-gnu" ./dotnet/dotnet candle/tools/net8.0/any/fhir-candle.dll --version
