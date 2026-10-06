"""Stamp one version into every file that carries it:  python scripts/set_version.py 1.0.42"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [
    ("package.json", r'"version": "[^"]*"', '"version": "{v}"'),
    ("src-tauri/tauri.conf.json", r'"version": "[^"]*"', '"version": "{v}"'),
    ("src-tauri/Cargo.toml", r'(?m)^version = "[^"]*"', 'version = "{v}"'),
    ("backend/version.py", r'VERSION = "[^"]*"', 'VERSION = "{v}"'),
]


def main(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"Version must look like 1.2.3, got {version!r}")
    for rel, pattern, template in TARGETS:
        path = ROOT / rel
        text, n = re.subn(pattern, template.format(v=version), path.read_text(encoding="utf-8"), count=1)
        if n != 1:
            sys.exit(f"No version found in {rel}")
        path.write_text(text, encoding="utf-8", newline="")
    print(f"Version set to {version}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
