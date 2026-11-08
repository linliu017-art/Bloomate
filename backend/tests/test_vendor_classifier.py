"""The false-positive classification must fail for changed bytes or paths."""
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('vendor_classifier', ROOT / 'scripts/verify_vendor_artifact.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_classification_does_not_allow_unknown_paths_or_modified_dependency(tmp_path):
    source = ROOT / 'frontend/node_modules/typescript/lib/typescript.js'
    target = tmp_path / 'frontend/.next/standalone/node_modules/typescript/lib/typescript.js'
    target.parent.mkdir(parents=True)
    (target.parents[1] / 'package.json').write_text(json.dumps({'version': '5.9.3'}))
    target.write_bytes(source.read_bytes())
    assert MODULE.recognized(target, tmp_path)
    target.write_bytes(target.read_bytes() + b'\nmodified-file')
    assert not MODULE.recognized(target, tmp_path)
    assert not MODULE.recognized(source, tmp_path)


def test_classification_does_not_allow_different_package_version(tmp_path):
    target = tmp_path / 'frontend/.next/standalone/node_modules/typescript/lib/typescript.js'
    target.parent.mkdir(parents=True)
    (target.parents[1] / 'package.json').write_text(json.dumps({'version': 'future-version'}))
    target.write_bytes((ROOT / 'frontend/node_modules/typescript/lib/typescript.js').read_bytes())
    assert not MODULE.recognized(target, tmp_path)
