"""Build/export on a connected host; load/start on an air-gapped host (Python stdlib)."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.yaml")]
BUNDLE_FILES = ("docker-compose.yaml", ".env.example", "README.md", "scripts/offline.py",
                "deploy/seccomp-bwrap.json", "deploy/MOBY-LICENSE", "deploy/README.md", "deploy/officecli.json")


def run(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "export", "load", "start"])
    bundle_root = ROOT if (ROOT / "images.tar").is_file() else ROOT / "offline"
    parser.add_argument("--directory", type=Path, default=bundle_root)
    args = parser.parse_args()
    folder = args.directory.resolve()
    if shutil.which("docker") is None:
        parser.error("Docker is required; no dependencies will be installed automatically")
    if args.action == "build":
        run([*COMPOSE, "config", "--quiet"])
        run([*COMPOSE, "build", "--pull"])
        run(["docker", "pull", "--platform", "linux/amd64", "redis:7.4-alpine"])
    elif args.action == "export":
        images = run([*COMPOSE, "config", "--images"], capture=True).splitlines()
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "images.tar"
        run(["docker", "save", "-o", str(archive), *images])
        ids = {name: run(["docker", "image", "inspect", "--format", "{{.Id}}", name], capture=True) for name in images}
        (folder / "manifest.json").write_text(json.dumps({"platform": "linux/amd64", "sha256": checksum(archive), "images": ids}, indent=2), encoding="utf-8")
        (folder / "scripts").mkdir(exist_ok=True)
        for name in BUNDLE_FILES:
            source, destination = ROOT / name, folder / name
            if source.resolve() != destination.resolve():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        print(f"Exported self-contained deployment bundle: {folder}. Copy the whole directory to the offline host.")
    elif args.action == "load":
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        archive = folder / "images.tar"
        if checksum(archive) != manifest["sha256"]:
            raise SystemExit("Image archive checksum mismatch")
        run(["docker", "load", "-i", str(archive)])
        for name, expected in manifest["images"].items():
            if run(["docker", "image", "inspect", "--format", "{{.Id}}", name], capture=True) != expected:
                raise SystemExit(f"Image ID mismatch: {name}")
    else:
        run([*COMPOSE, "up", "-d", "--no-build", "--pull", "never"])


if __name__ == "__main__":
    main()
