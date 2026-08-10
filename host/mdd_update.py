#!/usr/bin/env python3
"""Detached self-update runner for MDD Sim Gateway.

The WebUI publishes an update request; the host orchestrator stages a COPY of this script
under ``<data>/update/`` and launches it as a transient systemd unit (``systemd-run``).
Both indirections are required for the update to survive itself:

  - ``install.sh reload`` restarts the control plane AND the orchestrator, so an updater
    running inside either service would be killed halfway through;
  - the repository checkout this file ships in is overwritten while the updater runs, so it
    must execute from a copy outside the checkout.

Stdlib only (it runs before any requirements are reinstalled). Progress is published to
``<data>/orchestrator/update-status.json`` for the WebUI to poll.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

# Top-level entries that belong to the installation, not to a release: never replaced and
# never deleted (the default MDD_DATA_DIR lives at <repo>/data).
PRESERVE = {"data", ".env", ".git"}
# Locally-built artifacts nested inside release-managed directories. webui/dist is kept so
# the old UI keeps being served if the reload's WebUI rebuild fails; on success the rebuild
# replaces it wholesale anyway.
NESTED_PRESERVE = {"control": {".venv"}, "webui": {"node_modules", "dist"}}
BACKUP_EXCLUDE = {"data", ".git", ".venv", "node_modules", "__pycache__"}

VERSION_RE = re.compile(r"\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class UpdateError(RuntimeError):
    pass


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Status:
    def __init__(self, path: Path, target: str):
        self.path, self.target = path, target
        self.started = int(time.time())
        self.extra: dict = {}

    def publish(self, state: str, phase: str, **fields):
        self.extra.update(fields)
        atomic_json(self.path, {"state": state, "phase": phase, "target": self.target,
                                "started_at": self.started, "updated_at": int(time.time()),
                                **self.extra})


def download(url: str, destination: Path):
    request = urllib.request.Request(url, headers={"User-Agent": "mdd-sim-gateway-updater"})
    with urllib.request.urlopen(request, timeout=600) as response, open(destination, "wb") as out:
        shutil.copyfileobj(response, out)


def extract(archive: Path, destination: Path) -> Path:
    """Unpack the GitHub source tarball and return its single top-level directory."""
    with tarfile.open(archive, "r:gz") as tar:
        try:
            tar.extractall(destination, filter="data")
        except TypeError:  # Python without the extraction-filter backport
            base = destination.resolve()
            for member in tar.getmembers():
                target = (destination / member.name).resolve()
                if base != target and base not in target.parents:
                    raise UpdateError(f"unsafe path in release archive: {member.name}")
                if member.islnk() or member.issym():
                    raise UpdateError(f"link member in release archive: {member.name}")
            tar.extractall(destination)
    roots = [entry for entry in destination.iterdir() if entry.is_dir()]
    if len(roots) != 1 or not (roots[0] / "install.sh").is_file():
        raise UpdateError("release archive does not look like a gateway source tree")
    return roots[0]


def backup(repo: Path, data: Path, current: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = data / "backups" / f"pre-update-{current or 'unknown'}-{stamp}.tar.gz"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def keep(info: tarfile.TarInfo):
        return None if any(part in BACKUP_EXCLUDE for part in Path(info.name).parts) else info

    with tarfile.open(destination, "w:gz") as tar:
        tar.add(repo, arcname="mdd-sim-gateway", filter=keep)
    os.chmod(destination, 0o600)
    return destination


def apply_tree(source_root: Path, repo: Path):
    """Replace release-managed content in the checkout with the new release's files."""
    for entry in sorted(source_root.iterdir(), key=lambda item: item.name):
        if entry.name in PRESERVE:
            continue
        target = repo / entry.name
        if not entry.is_dir():
            if target.is_dir():
                shutil.rmtree(target)
            shutil.copy2(entry, target)
            continue
        preserved = NESTED_PRESERVE.get(entry.name) or set()
        if preserved and target.is_dir():
            for child in target.iterdir():
                if child.name in preserved:
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            for child in entry.iterdir():
                if child.name in preserved:
                    continue
                if child.is_dir():
                    shutil.copytree(child, target / child.name, symlinks=True)
                else:
                    shutil.copy2(child, target / child.name)
        else:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            shutil.copytree(entry, target, symlinks=True)


def perform(repo: Path, data: Path, version: str, repo_name: str, status: Status):
    if not VERSION_RE.fullmatch(version):
        raise UpdateError(f"invalid target version: {version!r}")
    if not REPOSITORY_RE.fullmatch(repo_name):
        raise UpdateError(f"invalid repository: {repo_name!r}")
    (data / "update").mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="mdd-update.", dir=str(data / "update")))
    try:
        url = f"https://github.com/{repo_name}/archive/refs/tags/v{version}.tar.gz"
        status.publish("running", "downloading", url=url)
        archive = staging / "release.tar.gz"
        download(url, archive)

        status.publish("running", "verifying")
        source_root = extract(archive, staging / "tree")
        version_file = source_root / "VERSION"
        packaged = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
        if packaged != version:
            raise UpdateError(f"release archive reports version {packaged!r}, expected {version!r}")

        status.publish("running", "backup")
        try:
            current = (repo / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            current = ""
        saved = backup(repo, data, current)

        status.publish("running", "applying", backup=str(saved))
        apply_tree(source_root, repo)

        # Reload rebuilds the WebUI + venv (or the control image in docker mode) and restarts
        # the control plane and orchestrator — this unit outlives both restarts.
        status.publish("running", "reloading")
        log_path = data / "update" / "reload.log"
        with open(log_path, "w", encoding="utf-8") as log:
            result = subprocess.run(["sh", str(repo / "install.sh"), "reload"],
                                    cwd=str(repo), stdout=log, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            with open(log_path, encoding="utf-8", errors="replace") as log:
                tail = "".join(log.readlines()[-40:])
            raise UpdateError(f"install.sh reload exited with {result.returncode}\n{tail}")
        status.publish("success", "done")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    data = args.data.resolve()
    status = Status(data / "orchestrator" / "update-status.json", args.version)
    try:
        perform(args.repo.resolve(), data, args.version, args.repository, status)
    except Exception as exc:  # published for the WebUI; the unit exit code is for journalctl
        status.publish("failed", "error", error=str(exc)[:4000])
        raise SystemExit(1)


if __name__ == "__main__":
    main()
