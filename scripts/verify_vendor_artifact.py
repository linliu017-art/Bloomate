"""Recognize two exact npm-published files with documented scanner false positives.

Only the generic pattern scan may use this classification. Actual credential
value scans still inspect every artifact, including these files. Any changed
byte, path or package version invalidates the classification.
Provenance: docs/评测/阶段4-依赖误报核验.json, npm tarball SHA512 == lockfile.
"""
import hashlib
import json
from pathlib import Path
import sys

KNOWN = {
    'next/dist/compiled/amphtml-validator/validator_wasm.js': (
        'next', '15.5.28', 'f1f4629ab36f555f5a9937a5cf00f0ef780bf025883c035d06389b151c896f26'),
    'typescript/lib/typescript.js': (
        'typescript', '5.9.3', '3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675'),
}


def recognized(path, root=None):
    root = root or Path(__file__).resolve().parents[1]
    base = root / 'frontend/.next/standalone/node_modules'
    try:
        path = Path(path).resolve()
        relative = path.relative_to(base.resolve()).as_posix()
        package, version, expected = KNOWN[relative]
        installed = json.loads((base / package / 'package.json').read_text())
        return installed['version'] == version and hashlib.sha256(path.read_bytes()).hexdigest() == expected
    except (OSError, ValueError, KeyError):
        return False


if __name__ == '__main__':
    raise SystemExit(0 if len(sys.argv) == 2 and recognized(sys.argv[1]) else 1)
