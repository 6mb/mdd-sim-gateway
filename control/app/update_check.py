"""Release checker + one-click update request publisher.

The control plane never applies files itself: ``request_apply`` publishes a request document
that the root host orchestrator picks up and hands to a detached ``systemd-run`` unit
(``host/mdd_update.py``), which downloads the tagged release, overlays the checkout and runs
``install.sh reload``. Progress comes back through ``update-status.json``.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .version import VERSION

DEFAULT_REPOSITORY = "MddIdd/mdd-sim-gateway"
_cache: tuple[float, dict] | None = None


def repository() -> str:
    return os.environ.get("MDD_UPDATE_REPOSITORY", DEFAULT_REPOSITORY).strip()


def _version_tuple(value: str) -> tuple[int, ...]:
    core = str(value).strip().removeprefix("v").split("-", 1)[0]
    try:
        return tuple(int(part) for part in core.split("."))
    except ValueError:
        return (0,)


def check(force: bool = False) -> dict:
    global _cache
    now = time.time()
    if not force and _cache and now - _cache[0] < 300:
        return dict(_cache[1])
    repository_name = repository()
    url = f"https://api.github.com/repos/{repository_name}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"mdd-sim-gateway/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(url, headers=headers)
    result = {"ok": False, "current": VERSION, "repository": repository_name,
              "update_available": False, "checked_at": int(now)}
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.load(response)
        latest = str(payload.get("tag_name") or "").removeprefix("v")
        result.update({
            "ok": bool(latest),
            "latest": latest,
            "update_available": _version_tuple(latest) > _version_tuple(VERSION),
            "release_url": str(payload.get("html_url") or ""),
            "published_at": str(payload.get("published_at") or ""),
            "notes": str(payload.get("body") or "")[:4000],
        })
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 404}:
            # Private repositories are intentionally invisible to GitHub's unauthenticated API.
            # Once the repository and a release are public, no GitHub account/token is needed.
            result["error"] = "No public release is available yet"
            result["error_code"] = "update.error.no_public_release"
        elif exc.code == 403:
            result["error"] = "GitHub update check was rate-limited"
            result["error_code"] = "update.error.rate_limited"
        else:
            result["error"] = f"GitHub returned HTTP {exc.code}"
            result["error_code"] = "update.error.github"
    except (OSError, ValueError, TypeError) as exc:
        result["error"] = f"Update service unavailable: {type(exc).__name__}"
        result["error_code"] = "update.error.unavailable"
    _cache = (now, result)
    return dict(result)


def _apply_paths() -> tuple[str, str]:
    from . import config as cfg
    root = os.path.join(cfg.DATA_DIR, "orchestrator")
    return os.path.join(root, "update-request.json"), os.path.join(root, "update-status.json")


def _write_private_json(path: str, value: dict):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def apply_status() -> dict:
    """Current self-update progress as published by the host-side updater."""
    request_path, status_path = _apply_paths()
    try:
        with open(status_path, encoding="utf-8") as handle:
            status = json.load(handle)
        if not isinstance(status, dict):
            status = {}
    except (OSError, ValueError):
        status = {}
    status.setdefault("state", "idle")
    try:
        with open(request_path, encoding="utf-8") as handle:
            requested_at = int((json.load(handle) or {}).get("requested_at") or 0)
        status["requested"] = True
        # An unconsumed request means the orchestrator is not picking work up (stopped or
        # never installed) — surface that instead of letting the UI spin forever.
        if time.time() - requested_at > 120:
            status["state"] = "stalled"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return status


def request_apply() -> dict:
    """Publish a one-click update request for the host orchestrator."""
    status = apply_status()
    if status.get("state") == "running" and time.time() - int(status.get("updated_at") or 0) < 3600:
        return {"ok": False, "error": "An update is already in progress",
                "error_code": "update.error.in_progress", "status": status}
    info = check(True)
    if not info.get("update_available"):
        return {"ok": False, "error": info.get("error") or "No update is available",
                "error_code": info.get("error_code") or "update.error.not_available"}
    request_path, status_path = _apply_paths()
    now = int(time.time())
    # Reset the visible status first so a stale success/failure from a previous run cannot be
    # mistaken for this run's outcome while the orchestrator picks the request up.
    _write_private_json(status_path, {"state": "running", "phase": "requested",
                                      "target": info["latest"], "updated_at": now})
    _write_private_json(request_path, {"version": info["latest"], "repository": repository(),
                                       "requested_at": now})
    return {"ok": True, "version": info["latest"]}
