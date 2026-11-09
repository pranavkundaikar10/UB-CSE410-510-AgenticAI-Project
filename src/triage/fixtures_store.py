"""Read-only access to registered incident bundles via the manifest."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

from triage import paths
from triage.errors import ChecksumMismatch, UnknownIncident
from triage.paths import safe_join
from triage.schemas import BundleFile, IncidentEntry, Manifest, Topology

# Read at call time so tests can point the store at a copy via monkeypatch.
FIXTURES_DIR: Path = paths.FIXTURES_DIR
MANIFEST_NAME = "manifest.json"
TOPOLOGY_NAME = "topology.json"
_INCIDENT_ID_RE = re.compile(r"^INC-\d{3}$")

Bundle = tuple[IncidentEntry, list[dict[str, Any]], list[dict[str, Any]]]
_BUNDLE_CACHE: dict[tuple[str, str], Bundle] = {}


def load_manifest() -> Manifest:
    """Read and validate fixtures/manifest.json."""
    path = safe_join(FIXTURES_DIR, MANIFEST_NAME)
    return Manifest.model_validate_json(path.read_bytes())


def load_topology() -> Topology:
    """Read and validate fixtures/topology.json."""
    path = safe_join(FIXTURES_DIR, TOPOLOGY_NAME)
    return Topology.model_validate_json(path.read_bytes())


def get_incident(incident_id: str) -> IncidentEntry:
    """Return the manifest entry, or raise UnknownIncident for malformed/unregistered IDs."""
    if not isinstance(incident_id, str) or not _INCIDENT_ID_RE.fullmatch(incident_id):
        raise UnknownIncident(f"malformed incident ID: {incident_id!r}")
    for entry in load_manifest().incidents:
        if entry.incident_id == incident_id:
            return entry
    raise UnknownIncident(f"incident not registered: {incident_id}")


def _read_verified(ref: BundleFile) -> bytes:
    """Read a bundle file through safe_join and check its SHA-256 before any parsing."""
    data = safe_join(FIXTURES_DIR, ref.path).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != ref.sha256:
        raise ChecksumMismatch(f"{ref.path}: expected {ref.sha256}, got {actual}")
    return data


def _parse_logs(data: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def _parse_metrics(data: bytes) -> list[dict[str, Any]]:
    return [
        {**row, "value": float(row["value"])}
        for row in csv.DictReader(io.StringIO(data.decode("utf-8")))
    ]


def load_bundle(incident_id: str) -> Bundle:
    """Return (entry, logs, metrics) for a registered incident, verified and cached.

    Callers get fresh lists of shallow-copied records, so mutating them never
    alters the cache. The cache key includes the fixtures directory.
    """
    entry = get_incident(incident_id)
    key = (str(Path(FIXTURES_DIR).resolve()), incident_id)
    if key not in _BUNDLE_CACHE:
        logs_bytes = _read_verified(entry.logs)
        metrics_bytes = _read_verified(entry.metrics)
        _BUNDLE_CACHE[key] = (entry, _parse_logs(logs_bytes), _parse_metrics(metrics_bytes))
    cached_entry, logs, metrics = _BUNDLE_CACHE[key]
    return cached_entry, [dict(r) for r in logs], [dict(r) for r in metrics]


def clear_cache() -> None:
    """Drop all cached bundles."""
    _BUNDLE_CACHE.clear()
