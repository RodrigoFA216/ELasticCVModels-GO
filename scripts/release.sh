#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
GO=${GO:-go}
release_os=$("$GO" env GOOS)
release_arch=$("$GO" env GOARCH)
release_dir=$(mktemp -d "${TMPDIR:-/tmp}/kinich-release.XXXXXX")
trap 'rm -rf "$release_dir"' EXIT INT TERM
mkdir -p "$release_dir/kinich/bin" artifacts
"$GO" build -trimpath -ldflags='-s -w -X main.version=0.1.0-local' -o "$release_dir/kinich/bin/kinichd" ./cmd/kinichd
cp -R worker docs models proto packaging scripts "$release_dir/kinich/"
cp README.md go.mod go.sum "$release_dir/kinich/"
find "$release_dir" -type d -name __pycache__ -exec rm -rf {} +
python3 - "$release_dir/kinich" "$release_os" "$release_arch" <<'PY'
from pathlib import Path
import hashlib,json,platform,sys,subprocess
root=Path(sys.argv[1])
files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}
(root/'release-manifest.json').write_text(json.dumps({'source_commit':subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip() or 'uncommitted-local','version':'0.1.0-local','platform':sys.argv[2],'architecture':sys.argv[3],'validation':'local-only','files':files},indent=2)+'\n')
PY
COPYFILE_DISABLE=1 tar -czf "artifacts/kinich-0.1.0-local-${release_os}-${release_arch}.tar.gz" -C "$release_dir" kinich
printf 'Release local creado en artifacts/; perfiles CUDA y despliegue requieren aceptación propia.\n'
