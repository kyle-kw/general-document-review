import importlib.util
import json
from pathlib import Path
import sys

import pytest


def offline_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "offline.py"
    spec = importlib.util.spec_from_file_location("offline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_export_is_self_contained_and_load_verifies_before_docker(tmp_path, monkeypatch):
    module = offline_module()
    root = tmp_path / "source"
    (root / "scripts").mkdir(parents=True)
    for name in module.BUNDLE_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("test fixture", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.shutil, "which", lambda _: "docker")
    commands = []

    def run(args, *, capture=False):
        commands.append(args)
        if args[-1] == "--images":
            return "review-web:test\nreview-worker:test\nredis:test"
        if args[1] == "save":
            Path(args[args.index("-o") + 1]).write_bytes(b"archive fixture")
        if args[1:3] == ["image", "inspect"]:
            return "sha256:fixture"
        return ""

    monkeypatch.setattr(module, "run", run)
    monkeypatch.setattr(sys, "argv", ["offline.py", "export"])
    module.main()
    bundle = root / "offline"
    assert (bundle / "scripts/offline.py").is_file() and (bundle / "README.md").is_file()
    assert (bundle / "deploy/seccomp-bwrap.json").is_file()
    assert json.loads((bundle / "manifest.json").read_text())["sha256"] == module.checksum(bundle / "images.tar")
    monkeypatch.setattr(module, "ROOT", bundle)
    monkeypatch.setattr(sys, "argv", ["offline.py", "load"])
    module.main()
    assert any(args[1] == "load" for args in commands)
    commands.clear()
    (bundle / "images.tar").write_bytes(b"corrupted")
    with pytest.raises(SystemExit, match="checksum mismatch"):
        module.main()
    assert commands == []


def test_offline_start_never_builds_or_pulls(monkeypatch):
    module = offline_module()
    monkeypatch.setattr(module.shutil, "which", lambda _: "docker")
    calls = []
    monkeypatch.setattr(module, "run", lambda args, **_: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["offline.py", "start"])
    module.main()
    assert calls[0][-5:] == ["up", "-d", "--no-build", "--pull", "never"]
