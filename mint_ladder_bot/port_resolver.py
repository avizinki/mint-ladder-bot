"""
Resolve dashboard (and other) ports from env and workspace config/ports.yaml.
CEO directive: no hardcoded ports; use registry. Workspace root = parent of project root.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Optional

# Project root = directory containing mint_ladder_bot package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Workspace root = parent of project (repo containing config/ports.yaml).
_WORKSPACE_ROOT = _PROJECT_ROOT.parent
_PORTS_YAML = _WORKSPACE_ROOT / "config" / "ports.yaml"


def _load_registry(path: Path) -> dict[str, int]:
    """Load key: port from YAML (minimal parser)."""
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if k and v.isdigit():
                    out[k] = int(v)
    except Exception:
        pass
    return out


def _is_port_bound(host: str, port: int) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        sock.bind((host, port))
        sock.close()
        return False
    except OSError:
        return True


def resolve_dashboard_http_port() -> int:
    """
    Resolve dashboard HTTP port: PORT_DASHBOARD_HTTP or PORT_8765 (legacy) -> ports.yaml dashboard_http -> 6200.
    """
    for env_key in ("PORT_DASHBOARD_HTTP", "PORT_8765"):
        val = os.environ.get(env_key, "").strip()
        if val:
            try:
                return int(val)
            except ValueError:
                pass
    reg = _load_registry(_PORTS_YAML)
    return reg.get("dashboard_http", 6200)


def get_dashboard_bind_port(host: str = "127.0.0.1", preferred: Optional[int] = None) -> int:
    """
    Return a port to bind for dashboard: preferred if free, else first free in 6200..6299.
    """
    port = preferred if preferred is not None else resolve_dashboard_http_port()
    # If preferred is outside dashboard range, clamp to range for auto-shift.
    start = 6200 if port < 6200 or port > 6299 else port
    end = 6299
    for p in range(start, end + 1):
        if not _is_port_bound(host, p):
            return p
    # Fallback: return preferred even if bound (caller will get OSError).
    return port
