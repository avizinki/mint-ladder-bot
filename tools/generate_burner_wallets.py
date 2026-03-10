#!/usr/bin/env python3
"""
Generate N Solana burner keypairs and write config to burner_wallets.yaml.

Wallet states: ACTIVE, PAUSED, COOLDOWN, DISABLED, DRAINED.
Never logs or prints private keys; only pubkeys and wallet_ids.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Use solders (existing dep); str(Keypair) yields base58 secret for env
try:
    from solders.keypair import Keypair
except ImportError:
    print("solders is required; install with: pip install solders", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_STATE = "ACTIVE"
VALID_STATES = frozenset({"ACTIVE", "PAUSED", "COOLDOWN", "DISABLED", "DRAINED"})


def wallet_id_to_env_var(wallet_id: str) -> str:
    """e.g. burner_1 -> BURNER_1_PRIVATE_KEY_BASE58."""
    return f"{wallet_id.upper().replace('-', '_')}_PRIVATE_KEY_BASE58"


def generate_wallets(
    n: int,
    wallet_id_prefix: str = "burner",
    state: str = DEFAULT_STATE,
) -> list[dict]:
    """Generate n keypairs; return list of dicts with wallet_id, pubkey, state (no secret in dict)."""
    if state not in VALID_STATES:
        raise ValueError(f"state must be one of {sorted(VALID_STATES)}")
    out = []
    for i in range(1, n + 1):
        wallet_id = f"{wallet_id_prefix}_{i}"
        kp = Keypair()
        pubkey = str(kp.pubkey())
        out.append({
            "wallet_id": wallet_id,
            "pubkey": pubkey,
            "state": state,
        })
        # Keep secret only for env fragment; never log/print
        out[-1]["_secret_base58"] = str(kp)
    return out


def write_yaml(entries: list[dict], path: Path) -> None:
    """Write wallet list to YAML; strip internal _secret_base58."""
    to_write = []
    for e in entries:
        to_write.append({
            "wallet_id": e["wallet_id"],
            "pubkey": e["pubkey"],
            "state": e["state"],
        })
    data = {"wallets": to_write}
    path.parent.mkdir(parents=True, exist_ok=True)
    if yaml is None:
        # Minimal YAML write without PyYAML
        lines = ["wallets:"]
        for w in to_write:
            lines.append(f"  - wallet_id: {w['wallet_id']!r}")
            lines.append(f"    pubkey: {w['pubkey']!r}")
            lines.append(f"    state: {w['state']!r}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def write_env_fragment(entries: list[dict], path: Path) -> None:
    """Append .env.example fragment (variable names and placeholders only; no secrets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        "# Burner wallet keys (DO NOT COMMIT REAL KEYS)",
    ]
    for e in entries:
        env_var = wallet_id_to_env_var(e["wallet_id"])
        lines.append(f"# {e['wallet_id']} -> {e['pubkey']}")
        lines.append(f"# {env_var}=<base58-secret>")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_secrets_file(entries: list[dict], path: Path) -> None:
    """Write env lines with secrets to a file (caller must ensure file is gitignored)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Burner wallet secrets — add this file to .gitignore", ""]
    for e in entries:
        env_var = wallet_id_to_env_var(e["wallet_id"])
        lines.append(f"{env_var}={e['_secret_base58']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate N burner keypairs and write burner_wallets.yaml (and optional .env fragment)."
    )
    parser.add_argument(
        "n",
        type=int,
        help="Number of burner wallets to generate",
    )
    parser.add_argument(
        "--prefix",
        default="burner",
        help="Wallet ID prefix (default: burner -> burner_1, burner_2, ...)",
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE,
        choices=sorted(VALID_STATES),
        help=f"Initial state for generated wallets (default: {DEFAULT_STATE})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/burner_wallets.yaml"),
        help="Path to burner_wallets.yaml (default: config/burner_wallets.yaml)",
    )
    parser.add_argument(
        "--env-example",
        type=Path,
        default=None,
        help="If set, append env fragment (variable names only) to this file (e.g. .env.example)",
    )
    parser.add_argument(
        "--write-secrets-to",
        type=Path,
        default=None,
        help="If set, write PRIVATE_KEY_BASE58 lines to this file (add to .gitignore)",
    )
    args = parser.parse_args()

    if args.n < 1:
        print("n must be >= 1", file=sys.stderr)
        sys.exit(1)

    entries = generate_wallets(args.n, wallet_id_prefix=args.prefix, state=args.state)

    # Write YAML (pubkeys only in file)
    config_path = args.config
    write_yaml(entries, config_path)
    print(f"Wrote {len(entries)} wallet(s) to {config_path}")

    for e in entries:
        print(f"  {e['wallet_id']} -> {e['pubkey']} ({e['state']})")

    if args.env_example:
        write_env_fragment(entries, args.env_example)
        print(f"Appended env fragment to {args.env_example}")

    if args.write_secrets_to:
        write_secrets_file(entries, args.write_secrets_to)
        print(f"Wrote secret env lines to {args.write_secrets_to} (add to .gitignore)")

    # Ensure secrets are not retained in any logged structure
    for e in entries:
        e.pop("_secret_base58", None)


if __name__ == "__main__":
    main()
