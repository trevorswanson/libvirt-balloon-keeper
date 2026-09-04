#!/usr/bin/env bash
# Build a deterministic source bundle for review or Unraid transfer.
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
VERSION="${VERSION:-0.1.0}"
NAME="libvirt-balloon-keeper-${VERSION}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

install -d "$WORK/$NAME"
install -m 0644 "$ROOT/balloon_keeper.py" "$WORK/$NAME/"
install -m 0644 "$ROOT/web_server.py" "$WORK/$NAME/"
install -m 0644 "$ROOT/config.example.toml" "$WORK/$NAME/"
install -m 0644 "$ROOT/README.md" "$WORK/$NAME/"
python3 - "$ROOT/libvirt_balloon_keeper" "$WORK/$NAME/libvirt_balloon_keeper" <<'PY'
import shutil
import sys
shutil.copytree(sys.argv[1], sys.argv[2], ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
for path in __import__('pathlib').Path(sys.argv[2]).rglob('*'):
    path.chmod(0o750 if path.is_dir() else 0o640)
PY
install -d "$WORK/$NAME/unraid"
for file in lifecycle.sh install-cron.sh run-api.sh run-once.sh; do
    install -m 0750 "$ROOT/unraid/$file" "$WORK/$NAME/unraid/"
done
install -m 0644 "$ROOT/unraid/api.php" "$WORK/$NAME/unraid/"
install -m 0644 "$ROOT/unraid/libvirt-balloon-keeper.png" "$WORK/$NAME/unraid/"
install -m 0644 "$ROOT/unraid/libvirt-balloon-keeper.page" "$WORK/$NAME/unraid/"
install -d "$OUT"
python3 - "$WORK" "$OUT/libvirt-balloon-keeper.tar.gz" "$NAME" <<'PY'
import gzip
import sys
import tarfile
from pathlib import Path

root, output, name = sys.argv[1:]
source = Path(root, name)

def normalize(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info

with open(output, "wb") as raw:
    with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=0, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            paths = sorted((source, *source.rglob("*")), key=lambda path: path.relative_to(Path(root)).as_posix())
            for path in paths:
                archive.add(path, arcname=path.relative_to(Path(root)), recursive=False, filter=normalize)
PY
sha256sum "$OUT/libvirt-balloon-keeper.tar.gz" > "$OUT/libvirt-balloon-keeper.tar.gz.sha256"
printf 'built %s\n' "$OUT/libvirt-balloon-keeper.tar.gz"
