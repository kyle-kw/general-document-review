"""Build-time download of the fixed OfficeCLI release; never used at runtime."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen


def download(output: Path, manifest: Path) -> None:
    release = json.loads(manifest.read_text(encoding="utf-8"))
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="officecli-", dir=output.parent)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "wb") as target:
            request = Request(release["url"], headers={"User-Agent": "document-review-build"})
            with urlopen(request, timeout=120) as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError("OfficeCLI release exceeds the expected size limit")
                    digest.update(chunk)
                    target.write(chunk)
        if digest.hexdigest() != release["sha256"]:
            raise ValueError("OfficeCLI release SHA256 verification failed")
        os.chmod(temporary, 0o755)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("officecli.json"))
    args = parser.parse_args()
    download(args.output, args.manifest)


if __name__ == "__main__":
    main()
