#!/bin/sh
# Repair the bundled manager on the exact host releases assessed in November 2026.
# A future interpreter/runtime keeps the version its distributor ships.
set -eu
tool=$1
root=$2
case "$tool" in
  python)
    python="$root/bin/python3"
    [ "$("$python" -I -c 'import sys; print(sys.version.split()[0])')" = 3.15.2 ] || exit 0
    [ "$("$python" -I -c 'from importlib.metadata import version; print(version("pip"))')" = 26.2.2 ] || exit 0
    file=pip-26.3-py3-none-any.whl
    url="https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/$file"
    bits=256
    digest=f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13
    ;;
  node)
    node="$root/bin/node"
    npm="$root/lib/node_modules/npm/bin/npm-cli.js"
    [ "$("$node" --version)" = v26.13.1 ] || exit 0
    [ "$("$node" "$npm" --version)" = 11.20.0 ] || exit 0
    file=npm-11.21.3.tgz
    url="https://registry.npmjs.org/npm/-/$file"
    bits=512
    digest=e427fe8c4f9772d6e11ffce338ea970be8adcbb79e73f4e4938425dfcb5feb0fb586da76c183c7984e1cd51d44b370d8db6ec0735e3e48bae5609c2406271b4e
    ;;
  *) exit 2 ;;
esac

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
cd "$scratch"
curl --disable --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --max-time 120 --output "$file" "$url"
printf '%s  %s\n' "$digest" "$file" | shasum -a "$bits" --check --status

case "$tool" in
  python)
    "$python" -I -m pip --isolated --disable-pip-version-check install --prefix "$root" --no-index --no-deps --no-cache-dir "./$file"
    "$python" -I -c 'from importlib.metadata import version; assert version("pip") == "26.3"'
    ;;
  node)
    "$node" "$npm" install --global --prefix "$root" --cache "$scratch/cache" --offline --ignore-scripts --no-audit --no-fund "./$file"
    [ "$("$node" "$npm" --version)" = 11.21.3 ]
    ;;
esac
