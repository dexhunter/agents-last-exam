"""Install a verified, immutable Muse release without the self-updating launcher."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

_RELEASE_URL = "https://lookaside.facebook.com/lookaside/muse/download/"


def installed_version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "MUSE_NO_AUTO_UPDATE": "1", "MUSE_LOGIN": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.fullmatch(r"Muse Code \S+ \((\d+\.\d+\.\d+-R\d+(?:\.\d+)?)\)", result.stdout.strip())
    return match[1] if result.returncode == 0 and match else None


def install_release(version: str, *, is_linux: bool, directory: Path) -> Path:
    """Download to the install filesystem and verify size, SHA-256, and version."""
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        architecture = "x86"
    elif machine in {"aarch64", "arm64"}:
        architecture = "aarch64"
    else:
        raise RuntimeError(f"muse_code: unsupported architecture {machine!r}")
    target = f"{architecture}_{'linux' if is_linux else 'windows'}"
    url = f"{_RELEASE_URL}?channel=muse&version={version}&file=manifest.json"
    with urllib.request.urlopen(url, timeout=60) as response:
        manifest = json.load(response)
    if manifest.get("version") != version or manifest.get("checksum_algorithm") != "sha256":
        raise RuntimeError("muse_code: release manifest version or checksum algorithm mismatch")
    artifact = manifest.get("artifacts", {}).get(target, {})
    checksum, size = artifact.get("checksum", ""), artifact.get("size")
    if not re.fullmatch(r"[a-f0-9]{64}", checksum) or not isinstance(size, int) or size <= 0:
        raise RuntimeError(f"muse_code: invalid release artifact for {target}")
    if not str(artifact.get("url", "")).startswith("https://"):
        raise RuntimeError("muse_code: release artifact must use HTTPS")
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / ("muse" if is_linux else "muse.exe")
    descriptor, temporary = tempfile.mkstemp(prefix=".muse-", suffix=binary.suffix, dir=directory)
    staged = Path(temporary)
    try:
        digest = hashlib.sha256()
        downloaded = 0
        with (
            os.fdopen(descriptor, "wb") as output,
            urllib.request.urlopen(artifact["url"], timeout=120) as response,
        ):
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > size:
                    raise RuntimeError("muse_code: downloaded artifact exceeds manifest size")
                digest.update(chunk)
                output.write(chunk)
        if downloaded != size or digest.hexdigest() != checksum:
            raise RuntimeError("muse_code: release artifact size or SHA-256 mismatch")
        staged.chmod(0o755)
        if installed_version(str(staged)) != version:
            raise RuntimeError("muse_code: downloaded binary does not report the pinned release")
        staged.replace(binary)
        (directory / "release.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    finally:
        staged.unlink(missing_ok=True)
    return binary
