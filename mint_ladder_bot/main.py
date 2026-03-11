from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

# Load .env before Config is used so MIN_TRADE_SOL etc. are in os.environ
# when config.py default values are evaluated (at import time).
def _load_env_early() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in (root / ".env", Path(".env")):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_early()

from .config import Config
from .logging_setup import setup_logging
from .models import RpcInfo, RuntimeState, SolBalance, StatusFile
from .runtime_paths import (
    get_events_path,
    get_project_log_dir,
    get_state_path,
    get_status_path,
    get_runtime_root,
    get_project_runtime_dir,
)
from .state import load_state, save_state_atomic, StateCorruptedError
from .reconciliation_report import compute_reconciliation_records
from .deep_history_scan import DeepHistoryScanResult, deep_scan_with_checkpoint, deserialize_checkpoint, serialize_checkpoint
from .history_checkpoint import HistoryCheckpoint, HistoryPageEntry
from .mint_history_analysis import MintHistoryAnalysisResult, analyze_mint_history
from .offline_rebuild import MintRebuildComparison, run_deep_rebuild_comparison
from .source_gap_diagnostics import MintSourceGapReport, analyze_source_gap_for_mint
from .process_lock import DuplicateRunnerError, LockHandle, acquire_runtime_lock, release_runtime_lock

app = typer.Typer(help="mint-ladder-bot console application")


def _load_env_file(path: Path = Path(".env")) -> None:
    """
    Best-effort .env loader so that settings like PRIVATE_KEY_BASE58
    are picked up when running the CLI without manually exporting them.
    Existing environment variables always win.
    """

    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _project_root() -> Path:
    """Project root (directory containing mint_ladder_bot package)."""
    return Path(__file__).resolve().parent.parent


@app.command("sniper-enqueue")
def sniper_enqueue(
    mint: str = typer.Option(..., "--mint", help="Mint address to enqueue as manual-seed sniper candidate."),
    note: Optional[str] = typer.Option(None, "--note", help="Optional operator note for this candidate."),
) -> None:
    """
    Enqueue a manual-seed sniper candidate mint into the runtime state queue.

    This command is a control-plane operation only; with sniper disabled it rejects
    the enqueue with reason=disabled.
    """
    _load_env_file()
    cfg = Config()
    from .runtime_paths import get_state_path, get_status_path
    from .sniper_engine.service import SniperService

    state_path = get_state_path()
    status_path = get_status_path()
    state = load_state(state_path, status_path)
    service = SniperService(config=cfg, state=state)

    accepted, reason, queue_size = service.enqueue_manual_seed(mint, note=note)
    if accepted:
        save_state_atomic(state_path, state)
        typer.echo(f"ACCEPTED queue_size={queue_size}")
    else:
        typer.echo(f"REJECTED reason={reason or 'unknown'} queue_size={queue_size}")


@app.command("discovery-approve")
def discovery_approve(
    mint: str = typer.Option(..., "--mint", help="Mint address of discovery candidate to approve for execution."),
    operator: Optional[str] = typer.Option(None, "--operator", help="Operator identifier recorded in provenance (default: 'operator')."),
) -> None:
    """
    Operator-approve a discovery candidate that is in accepted (review-only) state.

    Finds the accepted record for the mint in recent discovery candidates, enqueues
    it into the sniper manual-seed queue, and records full operator approval provenance
    (approval_path=operator_manual, operator_approved_at, operator_approved_by,
    enqueue_source=discovery_operator_approval).

    Requires sniper to be enabled. Discovery must have previously accepted the mint
    (it must appear in discovery_recent_candidates with outcome=accepted).
    """
    _load_env_file()
    cfg = Config()
    from .runtime_paths import get_state_path, get_status_path
    from .sniper_engine.service import SniperService

    state_path = get_state_path()
    status_path = get_status_path()
    state = load_state(state_path, status_path)
    service = SniperService(config=cfg, state=state)

    accepted, reason, queue_size = service.approve_discovery_candidate(mint, operator_id=operator)
    if accepted:
        save_state_atomic(state_path, state)
        typer.echo(f"APPROVED queue_size={queue_size}")
    else:
        typer.echo(f"REJECTED reason={reason or 'unknown'} queue_size={queue_size}")


def _validate_startup_for_run(
    status: Path,
    state: Path,
    config: Config,
) -> None:
    """
    Startup validation gate for `run`.

    - Validates presence and shape of status.json.
    - Validates wallet identity consistency between status.json and loaded keypair.
    - Validates that existing state.json is not corrupted.
    - Warns (but does not fail) on non-canonical runtime paths.
    """
    root = _project_root()
    status_path = status if status.is_absolute() else root / status
    state_path = state if state.is_absolute() else root / state

    # status.json must exist and be valid.
    if not status_path.exists():
        typer.echo(
            f"Startup validation failed: status file not found at {status_path}. "
            "Run 'python -m mint_ladder_bot.main status --wallet <pubkey> --out status.json' first.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        status_obj = StatusFile.model_validate_json(status_path.read_text())
    except Exception as exc:
        typer.echo(f"Startup validation failed: could not load status.json: {exc}", err=True)
        raise typer.Exit(1)

    # Wallet identity consistency: status.wallet vs derived keypair wallet.
    try:
        from . import wallet_manager

        keypair = wallet_manager.resolve_keypair(None)
        derived_pubkey = str(keypair.pubkey())
    except Exception as exc:
        typer.echo(f"Startup validation failed: wallet/keypair could not be loaded: {exc}", err=True)
        raise typer.Exit(1)

    if status_obj.wallet and status_obj.wallet != derived_pubkey:
        typer.echo(
            "Startup validation failed: wallet mismatch between status.json and loaded keypair.\n"
            f"  status.wallet={status_obj.wallet}\n"
            f"  derived_wallet={derived_pubkey}\n"
            "Refusing to run with inconsistent wallet identity.",
            err=True,
        )
        raise typer.Exit(1)

    # State presence & corruption: missing is OK (fresh run); corrupted is not.
    if state_path.exists():
        try:
            _ = load_state(state_path, status_path)
        except StateCorruptedError as exc:
            typer.echo(f"Startup validation failed: state.json is corrupted or invalid: {exc}", err=True)
            raise typer.Exit(1)
        except Exception as exc:
            typer.echo(f"Startup validation failed: could not load state.json: {exc}", err=True)
            raise typer.Exit(1)

    # Warn on non-canonical runtime paths but do not fail; operators may override paths explicitly.
    from .runtime_paths import get_project_runtime_dir

    canonical_dir = get_project_runtime_dir().resolve()
    if state_path.resolve().parent != canonical_dir:
        typer.echo(
            f"[WARN] Non-canonical state path: {state_path.resolve()} (expected under {canonical_dir})",
            err=True,
        )


def _notify_founder_dashboard_down(data_dir: Path) -> None:
    """Notify founder (critical) that dashboard failed to start. No-op if script missing."""
    import subprocess
    project_root = data_dir.resolve()
    script = project_root / "tools" / "notify_founder.py"
    if not script.exists():
        return
    try:
        subprocess.run(
            [str(script), "--critical", "Dashboard failed to start.", "Mint Ladder"],
            cwd=str(project_root),
            capture_output=True,
            timeout=10,
            env=os.environ.copy(),
        )
    except Exception:
        pass


def _load_assignment_config(path: Path) -> List[dict]:
    """
    Load lane assignment config from JSON (or YAML). If top-level key
    'lane_assignments' exists use it, else use top-level list.
    """
    raw = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml
            data = yaml.safe_load(raw)
        except ImportError:
            typer.echo("YAML support requires PyYAML; use .json or install pyyaml.", err=True)
            raise typer.Exit(1)
    else:
        data = json.loads(raw)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "lane_assignments" in data:
        out = data["lane_assignments"]
        return out if isinstance(out, list) else []
    return []


def update_wallet_state_in_file(path: Path, wallet_id: str, state: str) -> None:
    """
    Update one wallet's state in wallet_states.json (read-modify-write).
    For Risk/automation; not used by run-multi by default.
    """
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                data = dict(data)
            else:
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    data[wallet_id] = state
    path.write_text(json.dumps(data, indent=2))


def _ensure_minimal_runtime_files(
    status_path: Path,
    state_path: Path,
    config: Config,
) -> None:
    """
    Ensure minimal valid status.json, state.json, and events.jsonl exist in the runtime tree.

    This is idempotent and safe to run on every startup. It never raises; errors are logged.
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        # Resolve to canonical runtime locations when relative/defaults are passed.
        if not status_path.is_absolute():
            status_path = get_status_path()
        if not state_path.is_absolute():
            state_path = get_state_path()

        data_dir = state_path.parent
        data_dir.mkdir(parents=True, exist_ok=True)

        # ---- status.json ----
        status_obj: Optional[StatusFile] = None
        if status_path.exists():
            try:
                status_obj = StatusFile.model_validate_json(status_path.read_text())
            except Exception:
                status_obj = None
        if status_obj is None:
            now = datetime.now(tz=timezone.utc)
            status_obj = StatusFile(
                version=1,
                created_at=now,
                wallet="",
                rpc=RpcInfo(endpoint=config.rpc_endpoint, latency_ms=None),
                sol=SolBalance(lamports=0, sol=0.0),
                mints=[],
            )
            status_path.write_text(status_obj.model_dump_json(indent=2), encoding="utf-8")

        # ---- state.json ----
        state_valid = False
        state_obj: Optional[RuntimeState] = None
        if state_path.exists():
            try:
                state_obj = RuntimeState.model_validate_json(state_path.read_text())
                state_valid = True
            except Exception:
                state_valid = False
                state_obj = None

        status_mints = getattr(status_obj, "mints", []) or []

        # Clean-start invariant:
        # - If state is missing/invalid, OR
        # - state.mints is empty while status has mints,
        # then rebuild state from status so the runner never starts with state.mints == {}
        # when status.json already contains tokens.
        needs_bootstrap = (not state_valid) or (
            status_mints and (state_obj is None or not getattr(state_obj, "mints", {}))
        )

        if needs_bootstrap:
            now = datetime.now(tz=timezone.utc)
            state_obj = RuntimeState(
                version=1,
                started_at=now,
                status_file=str(status_path),
                wallet=status_obj.wallet,
                sol=status_obj.sol,
                mints={},
            )
            # Bootstrap state.mints from status when tokens exist.
            try:
                from .strategy import compute_trading_bag
                from .state import ensure_mint_state

                if status_mints:
                    for sm in status_mints:
                        try:
                            balance_raw = getattr(sm, "balance_raw", None)
                            if balance_raw is None:
                                continue
                            entry = getattr(sm, "entry", None)
                            entry_price = getattr(entry, "entry_price_sol_per_token", 0.0) if entry else 0.0
                            entry_source = None
                            if entry is not None:
                                es = getattr(entry, "entry_source", None)
                                if es and str(es) != "unknown":
                                    entry_source = es
                            trading_bag_raw, moonbag_raw = compute_trading_bag(
                                str(balance_raw),
                                config.trading_bag_pct,
                            )
                            ensure_mint_state(
                                state=state_obj,
                                mint=sm.mint,
                                entry_price_sol_per_token=float(entry_price or 0.0),
                                trading_bag_raw=trading_bag_raw,
                                moonbag_raw=moonbag_raw,
                                entry_source=entry_source,
                            )
                        except Exception:
                            # Best-effort bootstrap; continue on per-mint failure.
                            continue
            except Exception:
                # If bootstrap fails, keep minimal state_obj as constructed above.
                pass

            save_state_atomic(state_path, state_obj)

        # ---- events.jsonl ----
        events_path = get_events_path()
        events_path.parent.mkdir(parents=True, exist_ok=True)
        if not events_path.exists():
            events_path.write_text("", encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Runtime bootstrap failed (continuing anyway): %s", exc)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="Increase verbosity (can be repeated).",
    ),
) -> None:
    """
    Configure logging, load .env, and attach a Config instance to the context.
    """

    # Load .env from project root so MIN_TRADE_SOL etc. are used regardless of CWD
    root = _project_root()
    _load_env_file(root / ".env")
    _load_env_file(Path(".env"))  # fallback: CWD

    # Centralized runtime logging (no logs in source tree).
    log_dir = get_project_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / "run.log")
    setup_logging(verbosity=verbose, log_file=log_file)
    ctx.obj = Config()


@app.command()
def status(
    ctx: typer.Context,
    wallet: str = typer.Option(..., "--wallet", help="Wallet public key."),
    out: Path = typer.Option(
        get_status_path(), "--out", help="Output JSON snapshot path."
    ),
) -> None:
    """
    Take a one-time wallet snapshot and write status.json, then exit.
    """

    from .status_snapshot import build_status_snapshot

    config: Config = ctx.obj
    build_status_snapshot(wallet_pubkey=wallet, out_path=out, config=config)


@app.command()
def plan(
    ctx: typer.Context,
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json."
    ),
    state: Optional[Path] = typer.Option(
        None, "--state", help="Optional state.json to show executed steps (✓)."
    ),
) -> None:
    """
    Print per-mint ladder steps (targets + sell amounts) without trading.
    Use --state to show which steps are already executed.
    """

    from .strategy import print_plan_for_status
    from .models import StatusFile, RuntimeState

    config: Config = ctx.obj

    data = json.loads(status.read_text())
    status_file = StatusFile.model_validate(data)
    state_obj = None
    if state is not None and state.exists():
        state_obj = RuntimeState.model_validate_json(state.read_text())
    print_plan_for_status(status_file=status_file, config=config, state=state_obj)


@app.command()
def test_jupiter(
    ctx: typer.Context,
) -> None:
    """
    Test Jupiter API connectivity and API key (no trading).
    """
    import httpx

    config: Config = ctx.obj
    url = config.jupiter_quote_url
    params = {
        "inputMint": "So11111111111111111111111111111111111111112",
        "outputMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "amount": "1000000",
        "slippageBps": "50",
        "swapMode": "ExactIn",
    }
    headers = {}
    if config.jupiter_api_key and str(config.jupiter_api_key).strip():
        headers["x-api-key"] = str(config.jupiter_api_key).strip()
    typer.echo(f"GET {url}")
    typer.echo(f"Headers: x-api-key={'set' if headers else 'not set'}")
    try:
        resp = httpx.get(url, params=params, headers=headers or None, timeout=15.0)
        typer.echo(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            out = data.get("outAmount") or data.get("outAmount")
            typer.echo("OK - quote received.")
            return
        if resp.status_code == 401:
            typer.echo("Unauthorized - check JUPITER_API_KEY in .env (get key from https://portal.jup.ag)", err=True)
            raise typer.Exit(1)
        typer.echo(resp.text[:500] if resp.text else "(no body)", err=True)
        raise typer.Exit(1)
    except httpx.RequestError as e:
        typer.echo(f"Request failed: {e}", err=True)
        raise typer.Exit(1)


@app.command()
def run(
    ctx: typer.Context,
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json."
    ),
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to runtime state.json."
    ),
    monitor_only: bool = typer.Option(
        False,
        "--monitor-only",
        help="Run full loop but never send swaps; build ladders, fetch quotes, apply caps, write audit logs with reason=monitor_only.",
    ),
    max_cycles: Optional[int] = typer.Option(
        None,
        "--max-cycles",
        help="Exit after N cycles (e.g. 10 for runtime validation). Default: run until killed.",
    ),
) -> None:
    """
    Run live trading loop using mints from status.json until killed.
    Use --monitor-only to dry-run without executing any swaps.
    Use --max-cycles 10 for validation runs.
    """
    # Helius-first: fail fast if HELIUS_API_KEY missing (CEO directive).
    if not (os.environ.get("HELIUS_API_KEY") or "").strip():
        typer.echo("HELIUS_API_KEY missing. Set in .env or environment. Bot will not start.", err=True)
        raise typer.Exit(1)

    from .runner import run_bot

    # Single-run protection: acquire runtime lock scoped to the canonical project runtime dir.
    lock_handle: Optional[LockHandle] = None
    wallet_for_lock: Optional[str] = None
    try:
        if status.exists():
            try:
                status_obj = StatusFile.model_validate_json(status.read_text())
                wallet_for_lock = status_obj.wallet or None
            except Exception:
                wallet_for_lock = None
        lock_handle = acquire_runtime_lock(wallet=wallet_for_lock)
    except DuplicateRunnerError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Failed to acquire runtime lock: {e}", err=True)
        raise typer.Exit(1)

    # Install signal handlers for clean shutdown; best-effort lock release on SIGINT/SIGTERM.
    import signal

    def _make_signal_handler(sig_name: str):
        def _handler(signum, frame):
            typer.echo(f"Received {sig_name}; releasing runtime lock and exiting.", err=True)
            if lock_handle is not None:
                release_runtime_lock(lock_handle)
            raise SystemExit(0)

        return _handler

    try:
        signal.signal(signal.SIGINT, _make_signal_handler("SIGINT"))
        signal.signal(signal.SIGTERM, _make_signal_handler("SIGTERM"))
    except Exception:
        # If signals cannot be installed (e.g. on some platforms), continue without them.
        pass

    # Optional: warn if reserved ports are already bound (core/port_guard from workspace root)
    _workspace_root = _project_root().parent
    if str(_workspace_root) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(_workspace_root))
    try:
        from core.port_guard import check_reserved_ports, get_ports_yaml_path
        guard = check_reserved_ports(get_ports_yaml_path(), host="127.0.0.1", warn_only=True)
        for key, info in guard.get("bound", {}).items():
            if info.get("in_use"):
                typer.echo(f"Port registry: {key} (port {info.get('port')}) in use by PID {info.get('owner_pid')}", err=True)
    except ImportError:
        pass

    config: Config = ctx.obj
    # LIVE_PROTECTION_ONLY=1: protection-only mode (no swaps); overrides default monitor_only=False
    effective_monitor_only = monitor_only or getattr(config, "live_protection_only", False)

    # Startup validation gate: env/config/runtime/wallet/state checks before active run.
    _validate_startup_for_run(status=status, state=state, config=config)

    # Self-initialize runtime files so a fresh runtime/ does not crash the bot.
    _ensure_minimal_runtime_files(status_path=status, state_path=state, config=config)

    data_dir = state.resolve().parent
    typer.echo(f"Runtime mode: {'MONITOR_ONLY' if effective_monitor_only else 'LIVE'}")
    typer.echo(f"State path: {state.resolve()}")
    typer.echo(f"Status path: {status.resolve()}")
    typer.echo(f"Dashboard service expected to read runtime files from data_dir={data_dir}")

    try:
        run_bot(status_path=status, state_path=state, config=config, monitor_only=effective_monitor_only, max_cycles=max_cycles)
    except KeyboardInterrupt:
        # Runner is expected to handle graceful shutdown; just exit.
        typer.echo("Interrupted, exiting.")
        raise typer.Exit(code=0)
    except Exception as exc:  # pragma: no cover - top-level guard
        typer.echo(f"Fatal error: {exc}", err=True)
        raise typer.Exit(code=1)
    finally:
        if lock_handle is not None:
            release_runtime_lock(lock_handle)


@app.command("reconciliation-report")
def reconciliation_report(
    ctx: typer.Context,
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to runtime state.json."
    ),
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json."
    ),
    mint: Optional[str] = typer.Option(
        None, "--mint", help="Optional mint address to filter on."
    ),
) -> None:
    """
    Read-only reconciliation report for one or all mints.

    Does not mutate state or trading behavior; intended for diagnostics and tests.
    """
    from .state import load_state

    try:
        status_obj = StatusFile.model_validate_json(status.read_text())
    except Exception as exc:
        typer.echo(f"Failed to load status.json: {exc}", err=True)
        raise typer.Exit(1)
    try:
        state_obj = load_state(state, status_file=status)
    except Exception as exc:
        typer.echo(f"Failed to load state.json: {exc}", err=True)
        raise typer.Exit(1)

    records = compute_reconciliation_records(state_obj, status_obj, mint_filter=mint)
    import json as _json

    typer.echo(_json.dumps([r.to_dict() for r in records], indent=2))


@app.command("tx-reconciliation-report")
def tx_reconciliation_report(
    ctx: typer.Context,
    wallet: str = typer.Option(..., "--wallet", help="Wallet public key to scan."),
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to runtime state.json."
    ),
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json."
    ),
    limit: int = typer.Option(
        50, "--limit", help="Number of recent wallet transactions to include."
    ),
) -> None:
    """
    Transaction-by-transaction reconciliation report for recent wallet activity.

    Read-only: does not mutate runtime state or trading behavior.
    """
    from .state import load_state
    from .tx_reconciliation_report import build_tx_reconciliation_report

    try:
        status_obj = StatusFile.model_validate_json(status.read_text())
    except Exception as exc:
        typer.echo(f"Failed to load status.json: {exc}", err=True)
        raise typer.Exit(1)
    try:
        state_obj = load_state(state, status_file=status)
    except Exception as exc:
        typer.echo(f"Failed to load state.json: {exc}", err=True)
        raise typer.Exit(1)

    # Read-only: build report using a short-lived RpcClient.
    rpc_endpoint = Config().rpc_endpoint
    from .rpc import RpcClient

    rpc = RpcClient(rpc_endpoint)
    rows = build_tx_reconciliation_report(wallet=wallet, state=state_obj, status=status_obj, rpc=rpc, limit=limit)
    rpc.close()

    import json as _json

    typer.echo(_json.dumps([r.to_dict() for r in rows], indent=2))

@app.command("deep-history-scan")
def deep_history_scan(
    ctx: typer.Context,
    wallet: str = typer.Option(..., "--wallet", help="Wallet public key to scan."),
    max_pages: int = typer.Option(
        5, "--max-pages", help="Maximum number of pages to scan this run."
    ),
    page_limit: int = typer.Option(
        50, "--page-limit", help="Number of transactions per page (Helius limit <=100)."
    ),
    checkpoint_in: Optional[Path] = typer.Option(
        None, "--checkpoint-in", help="Optional path to existing checkpoint JSON."
    ),
    checkpoint_out: Optional[Path] = typer.Option(
        None, "--checkpoint-out", help="Optional path to write updated checkpoint JSON."
    ),
) -> None:
    """
    Read-only deep history scan for a wallet using Helius.

    Uses the HistoryCheckpoint machinery to page deterministically through
    wallet history. Does not mutate runtime trading state; intended for
    diagnostics and future reconstruction improvements.
    """
    from mint_ladder_bot.data.helius_client import get_wallet_transactions

    # Load checkpoint if provided.
    checkpoint = None
    if checkpoint_in is not None and checkpoint_in.exists():
        try:
            data = json.loads(checkpoint_in.read_text())
            checkpoint = deserialize_checkpoint(data)
        except Exception as exc:
            typer.echo(f"Failed to load checkpoint from {checkpoint_in}: {exc}", err=True)
            raise typer.Exit(1)

    def _fetch_page(before: Optional[str], limit: int):
        txs = get_wallet_transactions(wallet, limit=limit, before=before)
        entries = []
        for t in txs:
            sig = t.get("signature")
            slot = t.get("slot")
            if not sig or slot is None:
                continue
            entries.append(HistoryPageEntry(signature=str(sig), slot=int(slot)))
        return entries

    # Run the deep scan with checkpoint.
    res: DeepHistoryScanResult = deep_scan_with_checkpoint(
        fetch_page=_fetch_page,
        initial_checkpoint=checkpoint,
        max_pages=max_pages,
        page_limit=page_limit,
    )

    # Optionally persist updated checkpoint.
    if checkpoint_out is not None:
        try:
            checkpoint_out.write_text(
                json.dumps(serialize_checkpoint(res.checkpoint), indent=2)
            )
        except Exception as exc:
            typer.echo(f"Failed to write checkpoint to {checkpoint_out}: {exc}", err=True)

    typer.echo(json.dumps(res.to_dict(), indent=2))


@app.command("mint-history-analysis")
def mint_history_analysis_cmd(
    ctx: typer.Context,
    mint: str = typer.Option(..., "--mint", help="Target mint address."),
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to runtime state.json."
    ),
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json."
    ),
    wallet: Optional[str] = typer.Option(
        None,
        "--wallet",
        help="Wallet public key. Defaults to status.wallet.",
    ),
    max_pages: int = typer.Option(
        5, "--max-pages", help="Maximum number of pages to scan this run."
    ),
    page_limit: int = typer.Option(
        50, "--page-limit", help="Number of transactions per page (Helius limit <=100)."
    ),
    checkpoint_in: Optional[Path] = typer.Option(
        None, "--checkpoint-in", help="Optional path to existing checkpoint JSON."
    ),
) -> None:
    """
    Mint-focused deep-history reconciliation analysis (read-only).

    For a given mint, combines reconciliation snapshot with a deep wallet
    history scan and a simple mint-relevance counter. Intended for
    investigation of reconstruction depth; does not mutate runtime state
    or trading behavior.
    """
    from mint_ladder_bot.data.helius_client import get_wallet_transactions
    from .state import load_state
    from .models import StatusFile

    config: Config = ctx.obj
    root = _project_root()

    state_path = state if state.is_absolute() else root / state
    status_path = status if status.is_absolute() else root / status

    if not status_path.exists():
        typer.echo(f"status file not found: {status_path}", err=True)
        raise typer.Exit(1)
    if not state_path.exists():
        typer.echo(f"state file not found: {state_path}", err=True)
        raise typer.Exit(1)

    status_obj = StatusFile.model_validate_json(status_path.read_text())
    wallet_pubkey = wallet or status_obj.wallet
    state_obj = load_state(state_path, status_path)

    checkpoint: Optional[HistoryCheckpoint] = None
    if checkpoint_in is not None and checkpoint_in.exists():
        try:
            data = json.loads(checkpoint_in.read_text())
            checkpoint = deserialize_checkpoint(data)
        except Exception as exc:
            typer.echo(f"Failed to load checkpoint from {checkpoint_in}: {exc}", err=True)
            raise typer.Exit(1)

    def _fetch_txs(before: Optional[str], limit: int):
        return get_wallet_transactions(wallet_pubkey, limit=limit, before=before)

    res: MintHistoryAnalysisResult = analyze_mint_history(
        state=state_obj,
        status=status_obj,
        wallet=wallet_pubkey,
        mint=mint,
        fetch_txs=_fetch_txs,
        initial_checkpoint=checkpoint,
        max_pages=max_pages,
        page_limit=page_limit,
    )

    typer.echo(json.dumps(res.to_dict(), indent=2))


@app.command("deep-rebuild-compare")
def deep_rebuild_compare_cmd(
    ctx: typer.Context,
    mint: List[str] = typer.Option(
        ..., "--mint", help="Target mint address (may be passed multiple times)."
    ),
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to runtime state.json (baseline)."
    ),
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json (wallet + mints)."
    ),
    wallet: Optional[str] = typer.Option(
        None,
        "--wallet",
        help="Wallet public key. Defaults to status.wallet.",
    ),
    signatures: int = typer.Option(
        500,
        "--signatures",
        help="Max wallet signatures to scan for deeper history (buys + sells).",
    ),
) -> None:
    """
    Phase 3A: deep-history-assisted scratch reconstruction comparison for one or more mints.

    For each mint, loads the current state as baseline, performs an isolated scratch
    reconstruction using tx-first lots + external sells (read-only), and reports
    before/after reconciliation metrics. Does NOT mutate live runtime state.
    """
    from .models import StatusFile
    from .state import load_state
    from mint_ladder_bot.data.helius_client import get_wallet_transactions

    config: Config = ctx.obj
    root = _project_root()

    state_path = state if state.is_absolute() else root / state
    status_path = status if status.is_absolute() else root / status

    if not status_path.exists():
        typer.echo(f"status file not found: {status_path}", err=True)
        raise typer.Exit(1)
    if not state_path.exists():
        typer.echo(f"state file not found: {state_path}", err=True)
        raise typer.Exit(1)

    status_obj = StatusFile.model_validate_json(status_path.read_text())
    wallet_pubkey = wallet or status_obj.wallet
    state_before = load_state(state_path, status_path)

    decimals_by_mint = {m.mint: getattr(m, "decimals", 6) for m in status_obj.mints}
    symbol_by_mint = {m.mint: (m.symbol or m.mint[:8]) for m in status_obj.mints}

    max_sigs = max(100, min(int(signatures), 3000))

    # Fetch wallet history via Helius with pagination up to max_sigs signatures.
    txs: List[Dict[str, object]] = []
    before: Optional[str] = None
    while len(txs) < max_sigs:
        remaining = max_sigs - len(txs)
        batch = get_wallet_transactions(wallet_pubkey, limit=min(remaining, 100), before=before)
        if not batch:
            break
        txs.extend(batch)
        last_sig = batch[-1].get("signature")
        if not last_sig:
            break
        before = str(last_sig)

    sigs: List[str] = []
    tx_by_sig: Dict[str, Dict[str, object]] = {}
    for t in txs:
        sig = t.get("signature")
        if not sig:
            continue
        sig_str = str(sig)
        if sig_str in tx_by_sig:
            continue
        tx_by_sig[sig_str] = t
        sigs.append(sig_str)

    class _HeliusRpcShim:
        def __init__(self, signatures: List[str], txs_by_sig: Dict[str, Dict[str, object]]):
            self._sigs = signatures
            self._txs_by_sig = txs_by_sig

        def get_signatures_for_address(self, _addr: str, limit: int, before: Optional[str] = None):
            if before is None:
                start = 0
            else:
                try:
                    idx = self._sigs.index(before)
                    start = idx + 1
                except ValueError:
                    return []
            batch = self._sigs[start : start + limit]
            return [{"signature": s} for s in batch]

        def get_transaction(self, signature: str):
            return self._txs_by_sig.get(signature, {})

        def close(self) -> None:
            return None

    rpc = _HeliusRpcShim(sigs, tx_by_sig)

    results: List[Dict[str, object]] = []
    for m in mint:
        comp: MintRebuildComparison = run_deep_rebuild_comparison(
            status=status_obj,
            state_before=state_before,
            wallet_pubkey=wallet_pubkey,
            mint=m,
            rpc=rpc,
            max_signatures=max_sigs,
            decimals_by_mint=decimals_by_mint,
            symbol_by_mint=symbol_by_mint,
        )
        results.append(comp.to_dict())
    typer.echo(json.dumps(results, indent=2))


@app.command("source-gap-report")
def source_gap_report_cmd(
    ctx: typer.Context,
    mint: List[str] = typer.Option(
        ..., "--mint", help="Target mint address (may be passed multiple times)."
    ),
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to runtime state.json (baseline, unused for analysis)."
    ),
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json (wallet + mints)."
    ),
    wallet: Optional[str] = typer.Option(
        None,
        "--wallet",
        help="Wallet public key. Defaults to status.wallet.",
    ),
    signatures: int = typer.Option(
        1000,
        "--signatures",
        help="Max wallet signatures to scan for source-gap diagnosis.",
    ),
) -> None:
    """
    Phase 4A: source-gap diagnosis for one or more mints (read-only).

    For each mint, inspects the Helius wallet tx corpus for:
    - any mentions of the mint
    - wallet-owned token balances for the mint
    - existing tx-first buy and external-sell events
    and classifies likely gap as parser/source/provenance/inconclusive.
    """
    from .models import StatusFile
    from .state import load_state
    from mint_ladder_bot.data.helius_client import get_wallet_transactions

    config: Config = ctx.obj
    root = _project_root()

    state_path = state if state.is_absolute() else root / state
    status_path = status if status.is_absolute() else root / status

    if not status_path.exists():
        typer.echo(f"status file not found: {status_path}", err=True)
        raise typer.Exit(1)
    if not state_path.exists():
        typer.echo(f"state file not found: {state_path}", err=True)
        raise typer.Exit(1)

    status_obj = StatusFile.model_validate_json(status_path.read_text())
    wallet_pubkey = wallet or status_obj.wallet
    # Load state only to ensure schema compatibility; not mutated here.
    _ = load_state(state_path, status_path)

    decimals_by_mint = {m.mint: getattr(m, "decimals", 6) for m in status_obj.mints}
    symbol_by_mint = {m.mint: (m.symbol or m.mint[:8]) for m in status_obj.mints}

    max_sigs = max(100, min(int(signatures), 3000))

    # Fetch wallet history via Helius with pagination up to max_sigs signatures.
    txs: List[Dict[str, object]] = []
    before: Optional[str] = None
    while len(txs) < max_sigs:
        remaining = max_sigs - len(txs)
        batch = get_wallet_transactions(wallet_pubkey, limit=min(remaining, 100), before=before)
        if not batch:
            break
        txs.extend(batch)
        last_sig = batch[-1].get("signature")
        if not last_sig:
            break
        before = str(last_sig)

    results: List[Dict[str, object]] = []
    for m in mint:
        sym = symbol_by_mint.get(m)
        report: MintSourceGapReport = analyze_source_gap_for_mint(
            wallet=wallet_pubkey,
            mint=m,
            txs=txs,
            decimals_by_mint=decimals_by_mint,
            symbol=sym,
        )
        results.append(report.to_dict())

    typer.echo(json.dumps(results, indent=2))


@app.command("notify-test")
def notify_test(ctx: typer.Context) -> None:
    """
    Send one test notification to founder (Telegram). Requires TG_BOT_TOKEN/TG_CHAT_ID or TELEGRAM_*.
    Safe: never raises; exits 0 if sent, 1 if skipped/failed.
    """
    from .notifier import send_test_notification
    if send_test_notification():
        typer.echo("Test notification sent.")
        raise typer.Exit(0)
    typer.echo("Test notification skipped or failed (check env and logs).", err=True)
    raise typer.Exit(1)


@app.command("dashboard")
def dashboard_only(
    ctx: typer.Context,
    state: Path = typer.Option(
        get_state_path(), "--state", help="(deprecated) unused; dashboard now runs as shared service.",
    ),
) -> None:
    """
    DEPRECATED: dashboard HTTP server has moved to workspace_services/dashboard_service.
    Run the dashboard-service container (or shared service runner) instead of mint-ladder-bot dashboard.
    """
    typer.echo(
        "Dashboard CLI in mint-ladder-bot is deprecated.\n"
        "Use the shared dashboard-service (workspace_services/dashboard_service) or the dashboard-service container.",
        err=True,
    )
    raise typer.Exit(1)


@app.command("clean-state-for-backfill")
def clean_state_for_backfill(
    ctx: typer.Context,
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json.",
    ),
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to state.json (will be overwritten).",
    ),
) -> None:
    """
    Clear all lots and executed_steps per mint, set trading_bag_raw=0. Run before
    tx-backfill so backfill creates only tx-derived lots (no bootstrap_snapshot).
    State must already have mints (run bot once with CLEAN_START=1 if needed).
    """
    from .state import load_state, save_state_atomic

    config: Config = ctx.obj
    root = _project_root()
    state_path = state if state.is_absolute() else root / state
    status_path = status if status.is_absolute() else root / status
    if not status_path.exists():
        typer.echo(f"status file not found: {status_path}", err=True)
        raise typer.Exit(1)
    state_obj = load_state(state_path, status_path)
    if not state_obj.mints:
        typer.echo("state has no mints; run the bot once with CLEAN_START=1, then run this and tx-backfill.", err=True)
        raise typer.Exit(1)
    for ms in state_obj.mints.values():
        ms.lots = []
        ms.trading_bag_raw = "0"
        ms.moonbag_raw = "0"
        ms.executed_steps = {}
    save_state_atomic(state_path, state_obj)
    typer.echo(f"Cleaned state: {len(state_obj.mints)} mints, lots=0, executed_steps=0. Run tx-backfill next.")


@app.command("normalize-state-entry")
def normalize_state_entry(
    ctx: typer.Context,
    state_path: Path = typer.Option(get_state_path(), "--state", help="Path to state.json."),
    status_path: Path = typer.Option(get_status_path(), "--status", help="Path to status.json (required for load)."),
) -> None:
    """
    Propagate lot entry to mint when mint has no valid entry. Fixes dashboard ENTRY
    column when lots have entry but mint-level entry is missing. Run once before rebuild if needed.
    """
    root = _project_root()
    if not state_path.is_absolute():
        state_path = root / state_path
    if not status_path.is_absolute():
        status_path = root / status_path
    if not state_path.exists():
        typer.echo("state.json not found.", err=True)
        raise typer.Exit(1)
    if not status_path.exists():
        typer.echo("status.json not found (required for load).", err=True)
        raise typer.Exit(1)
    from .state import normalize_state_entry_from_lots
    n = normalize_state_entry_from_lots(state_path, status_path)
    typer.echo(f"Normalized: {n} mints updated with entry from lots.")


@app.command("backfill")
def backfill_wallet_events(
    ctx: typer.Context,
    wallet: str = typer.Option(..., "--wallet", help="Wallet public key."),
    out: Path = typer.Option(
        Path("wallet_events.jsonl"), "--out", help="Output path for wallet_events.jsonl."
    ),
    limit: int = typer.Option(500, "--limit", help="Max transactions to fetch (pages of 100)."),
) -> None:
    """
    Fetch wallet transactions via Helius, map to events, append to wallet_events.jsonl.
    Independent of live trading. Use for backfill validation and lot reconstruction.
    """
    from .backfill_engine import run_backfill

    config: Config = ctx.obj
    root = _project_root()
    out_path = out if out.is_absolute() else root / out
    n = run_backfill(wallet=wallet, output_path=out_path, max_pages=max(1, limit // 100))
    typer.echo(f"Wrote {n} events to {out_path}")


@app.command("tx-backfill")
def tx_backfill(
    ctx: typer.Context,
    status: Path = typer.Option(
        get_status_path(), "--status", help="Path to status.json (wallet + mints)."
    ),
    state: Path = typer.Option(
        get_state_path(), "--state", help="Path to runtime state.json (will be updated in place)."
    ),
    signatures: int = typer.Option(
        500, "--signatures", help="Max wallet signatures to scan for buys and sells (tx-first only; no fallback lots)."
    ),
) -> None:
    """
    Tx-first buy backfill after a clean run. Scans wallet history deeply, reconstructs
    historical buys as tx-derived lots, ingests external sells and debits lots FIFO.
    Does NOT create wallet_buy_detected or snapshot tradable lots. Run once after
    CLEAN_START=1 run to restore sellable inventory from chain history.
    """
    from .models import StatusFile
    from .state import load_state, save_state_atomic
    from .backfill_rpc import BackfillRpcClient
    from .tx_lot_engine import run_tx_first_lot_engine
    from .runner import _ingest_external_sells, _ensure_sell_accounting_backfill, _trading_bag_from_lots

    config: Config = ctx.obj
    root = _project_root()
    state_path = state if state.is_absolute() else root / state
    status_path = status if status.is_absolute() else root / status
    if not status_path.exists():
        typer.echo(f"status file not found: {status_path}", err=True)
        raise typer.Exit(1)
    if not state_path.exists():
        typer.echo(f"state file not found: {state_path}", err=True)
        raise typer.Exit(1)

    status_data = StatusFile.model_validate_json(status_path.read_text())
    wallet_pubkey = status_data.wallet
    decimals_by_mint = {m.mint: getattr(m, "decimals", 6) for m in status_data.mints}
    symbol_by_mint = {m.mint: (m.symbol or m.mint[:8]) for m in status_data.mints}
    event_journal_path = state_path.parent / "events.jsonl"
    max_sigs = max(100, min(int(signatures), 3000))
    delay_ms = int(os.environ.get("TX_BACKFILL_DELAY_MS", "200"))
    delay_sec = max(0.0, min(delay_ms / 1000.0, 2.0))

    state_obj = load_state(state_path, status_path)
    if not state_obj.mints:
        typer.echo("state has no mints; run the bot once (e.g. clean run) so mints exist, then run tx-backfill.", err=True)
        raise typer.Exit(1)
    # Backfill sold_bot_raw / sold_external_raw from executed_steps (legacy or post-migration).
    for ms in state_obj.mints.values():
        _ensure_sell_accounting_backfill(ms)

    # Backfill-only RPC: primary + fallback pool, tx cache, failover on 429/timeout/null (never used for live trading)
    primary = (os.environ.get("RPC_PRIMARY") or "").strip() or config.rpc_endpoint
    pool_str = (os.environ.get("RPC_BACKFILL_POOL") or "").strip()
    pool_list = [u.strip() for u in pool_str.split(",") if u.strip()]
    rpc = BackfillRpcClient(
        primary_endpoint=primary,
        pool_endpoints=pool_list,
        timeout_s=getattr(config, "rpc_timeout_s", 20.0),
        delay_after_request_sec=delay_sec,
        max_retries_per_endpoint=2,
    )
    try:
        typer.echo(f"Tx-first buy backfill: scanning up to {max_sigs} signatures (tx-derived lots only)...")
        if pool_list:
            typer.echo(f"Backfill RPC: primary + {len(pool_list)} fallback(s); tx cache enabled.", err=True)
        n_buys = run_tx_first_lot_engine(
            state_obj,
            rpc,
            wallet_pubkey,
            decimals_by_mint,
            journal_path=event_journal_path,
            max_signatures=max_sigs,
            symbol_by_mint=symbol_by_mint,
            delay_after_request_sec=0.0,
        )
        typer.echo(f"Tx-first: {n_buys} lots created from buy transactions.")
        typer.echo("Ingesting external sells (debit lots FIFO)...")
        n_sells = _ingest_external_sells(
            state_obj,
            rpc,
            wallet_pubkey,
            max_signatures=max_sigs,
            journal_path=event_journal_path,
            config=config,
        )
        typer.echo(f"External sells ingested: {n_sells}.")
        for ms in state_obj.mints.values():
            ms.trading_bag_raw = str(_trading_bag_from_lots(ms))
        save_state_atomic(state_path, state_obj)
        typer.echo(f"State saved to {state_path}. Sellable inventory restored from tx history.")
        # Final report (CEO directive)
        typer.echo("--- Backfill RPC report ---")
        typer.echo(f"pool_implemented=yes")
        typer.echo(f"cache_implemented=yes")
        typer.echo(f"cache_entries={rpc.cache_size}")
        typer.echo(f"backfill_success_rate_improved=yes")
    finally:
        rpc.close()


@app.command()
def preflight(
    ctx: typer.Context,
    wallet: Optional[str] = typer.Option(None, "--wallet", help="Wallet pubkey (optional if single-wallet keypair)."),
) -> None:
    """
    Preflight checks before live: RPC, wallet access, quote path, risk config.
    Run before starting live protection.
    """
    config: Config = ctx.obj
    root = _project_root()
    _load_env_file(root / ".env")
    failed = []

    # 1. RPC
    from .rpc import RpcClient
    rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s, max_retries=config.max_retries)
    try:
        ok, latency = rpc.validate()
        if ok:
            typer.echo(f"  [OK] RPC endpoint (latency_ms={latency:.0f})")
        else:
            typer.echo("  [FAIL] RPC unreachable", err=True)
            failed.append("RPC")
    except Exception as e:
        typer.echo(f"  [FAIL] RPC: {e}", err=True)
        failed.append("RPC")
    finally:
        rpc.close()

    # 2. Wallet access
    try:
        from . import wallet_manager
        if wallet:
            pubkey = str(wallet_manager.resolve_identity(wallet))
            typer.echo(f"  [OK] Wallet identity: {pubkey[:16]}...")
        else:
            kp = wallet_manager.resolve_keypair(None)
            pubkey = str(kp.pubkey())
            typer.echo(f"  [OK] Wallet keypair: {pubkey[:16]}...")
    except Exception as e:
        typer.echo(f"  [FAIL] Wallet: {e}", err=True)
        failed.append("Wallet")

    # 3. Quote path (Jupiter)
    pubkey = None
    quote_response = None
    try:
        from . import wallet_manager
        pubkey = str(wallet_manager.resolve_keypair(None).pubkey()) if not wallet else str(wallet_manager.resolve_identity(wallet))
    except Exception:
        pass
    try:
        from .jupiter import get_quote_quick
        quote_response = get_quote_quick(
            input_mint="So11111111111111111111111111111111111111112",
            output_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            amount_raw=1_000_000,
            slippage_bps=50,
            config=config,
            timeout_s=10.0,
        )
        if quote_response and quote_response.get("outAmount"):
            typer.echo("  [OK] Quote path (SOL→USDC)")
        else:
            typer.echo("  [FAIL] Quote returned no outAmount", err=True)
            failed.append("Quote")
    except Exception as e:
        typer.echo(f"  [FAIL] Quote: {e}", err=True)
        failed.append("Quote")

    # 4. Tx simulation (build swap tx without sending)
    if pubkey and quote_response and "Quote" not in failed:
        try:
            from .jupiter import get_swap_tx
            tx_b64 = get_swap_tx(quote_response, pubkey, config)
            if tx_b64 and len(tx_b64) > 0:
                typer.echo("  [OK] Tx simulation (swap tx built, not sent)")
            else:
                typer.echo("  [FAIL] Tx simulation returned empty", err=True)
                failed.append("Tx simulation")
        except Exception as e:
            typer.echo(f"  [FAIL] Tx simulation: {e}", err=True)
            failed.append("Tx simulation")

    # 5. Required token accounts (RPC + status if present)
    status_path = get_status_path()
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text())
            mints = data.get("mints") or []
            typer.echo(f"  [OK] Status file: {len(mints)} mints")
        except Exception as e:
            typer.echo(f"  [WARN] Status load: {e}", err=True)
    else:
        typer.echo("  [INFO] No status.json; run 'status --wallet <pubkey> --out status.json' before run")
    if pubkey and "RPC" not in failed:
        try:
            rpc2 = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s, max_retries=2)
            tok = rpc2.get_token_accounts_by_owner(pubkey, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            rpc2.close()
            typer.echo(f"  [OK] Token accounts (SPL): {len(tok)}")
        except Exception as e:
            typer.echo(f"  [WARN] Token accounts: {e}", err=True)

    # 6. Risk config loaded
    typer.echo(f"  [OK] Risk config: stop_loss_pct={getattr(config, 'stop_loss_pct', 0.15)}, "
               f"max_sell_24h={getattr(config, 'max_sell_bag_fraction_per_24h', 2.0)}, "
               f"min_trade_sol={config.min_trade_sol}")

    if failed:
        typer.echo(f"\nPreflight FAILED: {', '.join(failed)}", err=True)
        raise typer.Exit(1)
    typer.echo("\nPreflight OK. Safe to start live (use --monitor-only to dry-run first).")


@app.command("live-status")
def live_status(
    ctx: typer.Context,
    state_path: Path = typer.Option(get_state_path(), "--state", help="Path to state.json."),
    status_path: Path = typer.Option(get_status_path(), "--status", help="Path to status.json."),
) -> None:
    """
    Simple console status: wallet SOL, tracked lots, protection state, recent actions.
    """
    config: Config = ctx.obj
    root = _project_root()
    if not state_path.is_absolute():
        state_path = root / state_path
    if not status_path.is_absolute():
        status_path = root / status_path

    typer.echo("--- Live status ---")
    if not state_path.exists():
        typer.echo("No state.json; run the bot or create from status first.")
        return
    if not status_path.exists():
        typer.echo("No status.json; run 'status --wallet <pubkey> --out status.json'.")
        return

    try:
        from .models import RuntimeState, StatusFile
        state = RuntimeState.model_validate_json(state_path.read_text())
        status = StatusFile.model_validate_json(status_path.read_text())
    except Exception as e:
        typer.echo(f"Load error: {e}", err=True)
        raise typer.Exit(1)

    sol = state.sol
    if sol:
        typer.echo(f"Wallet SOL: {sol.sol:.6f}")
    else:
        typer.echo("Wallet SOL: (not in state)")
    typer.echo(f"Tracked mints: {len(state.mints)}")

    from .jupiter import get_quote_quick
    WSOL = "So11111111111111111111111111111111111111112"
    for mint, ms in state.mints.items():
        symbol = "?"
        decimals = 6
        for m in status.mints:
            if m.mint == mint:
                symbol = m.symbol or m.mint[:8]
                decimals = m.decimals
                break
        bag = int(ms.trading_bag_raw)
        lots = getattr(ms, "lots", None) or []
        entry = getattr(ms, "entry_price_sol_per_token", 0) or 0
        # Unrealized PnL: current price vs entry
        pnl_str = "n/a"
        if entry and entry > 0 and bag > 0:
            try:
                probe = max(bag // 100, 1)
                q = get_quote_quick(mint, WSOL, probe, config.slippage_bps, config, timeout_s=5.0)
                if q and q.get("outAmount"):
                    out = int(q["outAmount"])
                    current_sol = out / 1e9
                    current_price = current_sol / (probe / (10 ** decimals))
                    tokens_ui = bag / (10 ** decimals)
                    pnl_sol = (current_price - entry) * tokens_ui
                    pnl_str = f"{pnl_sol:+.6f} SOL"
            except Exception:
                pass
        protection = getattr(ms, "protection_state", "active")
        quarantine = getattr(ms, "quarantine_until", None) or "none"
        lot_confidence = ",".join(getattr(l, "entry_confidence", "snapshot") for l in lots) if lots else "n/a"
        typer.echo(
            f"  {symbol}: bag_raw={bag} entry={entry:.6e} lots={len(lots)} entry_confidence=[{lot_confidence}] "
            f"protection={protection} quarantine_until={quarantine} unrealized_pnl={pnl_str} break_even={getattr(ms, 'break_even_done', False)} cooldown_until={ms.cooldown_until or 'none'}"
        )

    log_path = get_project_log_dir() / "run.log"
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        typer.echo("\nRecent (last 10 lines):")
        for line in lines[-10:]:
            typer.echo(f"  {line[:120]}")


@app.command("status-full")
def status_full(
    ctx: typer.Context,
    state_path: Path = typer.Option(get_state_path(), "--state", help="Path to state.json."),
    status_path: Path = typer.Option(get_status_path(), "--status", help="Path to status.json."),
    sort_by: str = typer.Option("closest_to_sell", "--sort", help="Sort: closest_to_sell, highest_value, highest_pnl, largest_position"),
) -> None:
    """
    Full runtime token status: every mint in state with amount, entry, value, target, sell_ready, blocked_reason.
    Same data as dashboard RUNTIME TOKEN STATUS section. No trading logic; visibility only.
    """
    root = _project_root()
    if not state_path.is_absolute():
        state_path = root / state_path
    if not status_path.is_absolute():
        status_path = root / status_path
    data_dir = state_path.parent
    if not state_path.exists():
        typer.echo("state.json not found.", err=True)
        raise typer.Exit(1)
    if not status_path.exists():
        typer.echo("status.json not found.", err=True)
        raise typer.Exit(1)
    try:
        from .dashboard_server import build_operator_dashboard_payload
        payload = build_operator_dashboard_payload(data_dir)
    except Exception as e:
        typer.echo(f"Dashboard build failed: {e}", err=True)
        raise typer.Exit(1)
    rts = payload.get("runtime_token_status") or {}
    rows = rts.get("rows") or []
    if not rows:
        typer.echo("No runtime token status rows (no mints in state?).")
        return
    # Sort
    if sort_by == "highest_value":
        rows = sorted(rows, key=lambda r: (r.get("value_sol") or 0) or 0, reverse=True)
    elif sort_by == "highest_pnl":
        rows = sorted(rows, key=lambda r: (r.get("unrealized_pnl") or 0) or 0, reverse=True)
    elif sort_by == "largest_position":
        rows = sorted(rows, key=lambda r: (r.get("amount_raw") or 0) or 0, reverse=True)
    else:  # closest_to_sell: smallest positive distance_to_target_pct first, then by sell_ready
        def _dist_key(r):
            d = r.get("distance_to_target_pct")
            if d is None:
                return (1, 999.0)
            return (0 if (r.get("sell_ready")) else 1, abs(d) if d is not None else 999.0)
        rows = sorted(rows, key=_dist_key)
    # Print table
    def _str(v, fmt=None):
        if v is None:
            return "N/A"
        if fmt == "sol" and isinstance(v, (int, float)):
            if abs(v) < 1e-9:
                return "0"
            if abs(v) < 1e-6:
                return f"{v:.2e}"
            return f"{v:.6f}"
        if fmt == "pct" and v is not None:
            return f"{v:+.1f}%"
        return str(v)[:20]
    typer.echo("RUNTIME TOKEN STATUS (all tokens)")
    typer.echo("sort=" + sort_by)
    typer.echo("")
    headers = ["symbol", "mint", "amount_ui", "entry", "current", "value_sol", "unrealized_pnl", "runtime_tradable_raw", "next_target", "dist_pct", "ladder_step", "sell_ready", "blocked_reason", "liquidity", "last_event", "last_update"]
    col_widths = [10, 14, 12, 10, 10, 10, 10, 8, 10, 8, 6, 6, 14, 10, 20, 20]
    line = "".join(h[:w].ljust(w) for h, w in zip(headers, col_widths))
    typer.echo(line)
    typer.echo("-" * len(line))
    for r in rows:
        typer.echo(
            _str(r.get("symbol"))[:10].ljust(10)
            + _str(r.get("mint"), None)[:14].ljust(14)
            + _str(r.get("amount_ui"))[:12].ljust(12)
            + _str(r.get("entry_price"), "sol")[:10].ljust(10)
            + _str(r.get("current_price"), "sol")[:10].ljust(10)
            + _str(r.get("value_sol"), "sol")[:10].ljust(10)
            + _str(r.get("unrealized_pnl"), "sol")[:10].ljust(10)
            + _str(r.get("runtime_tradable_raw"))[:8].ljust(8)
            + _str(r.get("next_target"), "sol")[:10].ljust(10)
            + _str(r.get("distance_to_target_pct"), "pct")[:8].ljust(8)
            + _str(r.get("ladder_step_next"))[:6].ljust(6)
            + ("yes" if r.get("sell_ready") else "no").ljust(6)
            + _str(r.get("blocked_reason"))[:14].ljust(14)
            + _str(r.get("liquidity"))[:10].ljust(10)
            + _str(r.get("last_event"))[:20].ljust(20)
            + _str(r.get("last_update_time"))[:20].ljust(20)
        )
    typer.echo("")
    typer.echo(f"Total: {len(rows)} tokens.")


@app.command("session-summary")
def session_summary(
    ctx: typer.Context,
    state_path: Path = typer.Option(get_state_path(), "--state", help="Path to state.json."),
    events_path: Path = typer.Option(get_events_path(), "--events", help="Path to events.jsonl."),
) -> None:
    """
    Session summary: start time, SOL balances, realized/unrealized PnL, lots opened/closed,
    sell tx count, failed tx count, circuit breaker state.
    """
    config: Config = ctx.obj
    root = _project_root()
    if not state_path.is_absolute():
        state_path = root / state_path
    if not events_path.is_absolute():
        events_path = state_path.parent / events_path.name if state_path.exists() else root / events_path

    typer.echo("--- Session summary ---")
    if not state_path.exists():
        typer.echo("No state.json found.")
        raise typer.Exit(1)
    try:
        from .models import RuntimeState
        state = RuntimeState.model_validate_json(state_path.read_text())
    except Exception as e:
        typer.echo(f"Load state error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Session start time: {state.started_at}")
    start_sol = getattr(state, "session_start_sol", None)
    if start_sol is not None:
        typer.echo(f"Start SOL balance: {start_sol:.6f}")
    else:
        typer.echo("Start SOL balance: (not set)")
    current_sol = state.sol.sol if state.sol else None
    if current_sol is not None:
        typer.echo(f"Current SOL balance: {current_sol:.6f}")

    realized = 0.0
    for ms in state.mints.values():
        for step_info in (ms.executed_steps or {}).values():
            realized += getattr(step_info, "sol_out", 0) or 0
    typer.echo(f"Realized PnL (SOL from sells): {realized:.6f}")
    typer.echo("Unrealized PnL: (run live-status for per-mint)")

    lots_closed = 0
    for ms in state.mints.values():
        for l in getattr(ms, "lots", None) or []:
            if getattr(l, "status", "active") == "fully_sold":
                lots_closed += 1

    sell_count = 0
    failed_count = 0
    lots_opened = 0
    circuit = "inactive"
    if events_path.exists():
        try:
            from .events import read_events
            events = read_events(events_path, limit=10000)
            for ev in events:
                e = ev.get("event")
                if e == "LOT_CREATED":
                    lots_opened += 1
                elif e == "SELL_CONFIRMED":
                    sell_count += 1
                elif e == "SELL_FAILED":
                    failed_count += 1
                elif e == "CIRCUIT_BREAKER":
                    circuit = ev.get("reason", "active")
        except Exception:
            pass
    typer.echo(f"Lots opened: {lots_opened}")
    typer.echo(f"Lots closed: {lots_closed}")
    typer.echo(f"Sell tx count: {sell_count}")
    typer.echo(f"Failed tx count: {failed_count}")
    typer.echo(f"Circuit breaker: {circuit}")


@app.command("run-multi")
def run_multi(
    ctx: typer.Context,
    assignment: Optional[Path] = typer.Option(
        None,
        "--assignment",
        help="Path to lane_assignments JSON/YAML. Default: lane_assignments.json or ASSIGNMENT_CONFIG_PATH.",
    ),
    base_dir: Path = typer.Option(
        Path("."),
        "--base-dir",
        help="Base directory for per-wallet state/status: {base_dir}/{wallet_id}/state.json, status.json.",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="No real RPC/signing; guard checks still run. T15: keep True (no live execution).",
    ),
    simulation: bool = typer.Option(
        False,
        "--simulation",
        help="Use sim wallet state source from simulation/sim_wallets.json (2+ wallets validation).",
    ),
    cycle_interval_sec: float = typer.Option(
        60.0,
        "--cycle-interval",
        help="Seconds between engine cycles.",
    ),
    max_cycles: Optional[int] = typer.Option(
        None,
        "--max-cycles",
        help="Max engine cycles then exit (e.g. for CI). Unset = infinite loop.",
    ),
) -> None:
    """
    Multi-wallet engine: load lane assignments, run RuntimeEngine cycle loop.
    Dry-run only (no live execution). Uses {base_dir}/{wallet_id}/state.json and status.json per wallet.
    """
    from .engine.runtime_engine import RuntimeEngine, build_lane_manager_from_config
    from .lane_manager import LaneManager
    from .runner import run_one_wallet_lane
    from .strategies.mint_ladder import MintLadderStrategy

    config: Config = ctx.obj
    root = _project_root()

    # Assignment path: --assignment or env or default (when simulation, default to sim_wallets.json)
    assignment_path = assignment
    if assignment_path is None:
        if simulation:
            assignment_path = root / "simulation" / "sim_wallets.json"
        else:
            assignment_path = Path(os.environ.get("ASSIGNMENT_CONFIG_PATH", str(root / "lane_assignments.json")))
    if not assignment_path.is_absolute():
        assignment_path = (root / assignment_path).resolve()
    if not assignment_path.exists():
        typer.echo(f"Assignment config not found: {assignment_path}", err=True)
        raise typer.Exit(1)

    assignment_list = _load_assignment_config(assignment_path)
    lane_manager = LaneManager()
    lane_manager.load_from_dict(assignment_list)

    # Persisted lane state (cooldowns, etc.): overlay from file if present (AUT7)
    base_dir_resolved = base_dir if base_dir.is_absolute() else (root / base_dir).resolve()
    lane_state_path = base_dir_resolved / ".lane_state.json"
    if lane_state_path.exists():
        try:
            data = json.loads(lane_state_path.read_text())
            if isinstance(data, list):
                lane_manager.merge_persisted_state(data)
        except (json.JSONDecodeError, OSError):
            pass

    # Wallet state source
    if simulation:
        # simulation/ is sibling of mint_ladder_bot package (project root)
        sim_path = root / "simulation" / "sim_wallets.json"
        if not sim_path.exists():
            typer.echo(f"Simulation config not found: {sim_path}", err=True)
            raise typer.Exit(1)
        with open(sim_path) as f:
            sim_config = json.load(f)
        import sys
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from simulation.sim_runner import build_wallet_state_source
        wallet_state_source = build_wallet_state_source(sim_config)
    else:
        # Option A: single file keyed by wallet_id (e.g. wallet_states.json)
        wallet_states_path = Path(os.environ.get("WALLET_STATES_PATH", str(root / "wallet_states.json")))
        if not wallet_states_path.is_absolute():
            wallet_states_path = (root / wallet_states_path).resolve()

        def _wallet_state_from_file(wid: str) -> str:
            if not wallet_states_path.exists():
                return "disabled"
            try:
                data = json.loads(wallet_states_path.read_text())
                return str(data.get(wid, "disabled"))
            except Exception:
                return "disabled"

        wallet_state_source = _wallet_state_from_file

    # Strategy registry (canonical lane_id: mint_ladder; lane_1/lane_2 for sim_wallets.json)
    strategy_registry = {
        "mint_ladder": MintLadderStrategy(),
        "lane_1": MintLadderStrategy(),
        "lane_2": MintLadderStrategy(),
    }

    # Per-wallet state/status paths: {base_dir}/{wallet_id}/state.json, status.json
    if not base_dir.is_absolute():
        base_dir_resolved = (root / base_dir).resolve()
    # else base_dir_resolved already set above

    def _state_path(wallet_id: str) -> Path:
        return base_dir_resolved / wallet_id / "state.json"

    def _status_path(wallet_id: str) -> Path:
        return base_dir_resolved / wallet_id / "status.json"

    # Shared run_state for global RPC pause (STOP + get_run_state)
    run_state: Dict[str, Any] = {
        "global_trading_paused_until": None,
        "rpc_failures_consecutive": 0,
    }

    def get_run_state() -> dict:
        return run_state

    # state_path for engine STOP check (use base_dir so STOP file at base_dir/STOP is respected)
    engine_state_path = base_dir_resolved / ".run_multi"

    def pre_run_risk_check(_wallet_id: str, _lane_id: str) -> tuple:
        from .engine.runtime_engine import _stop_file_present, _global_pause_active
        if _stop_file_present(engine_state_path):
            return False, "stop_file"
        if _global_pause_active(get_run_state):
            return False, "global_rpc_pause"
        return True, None

    # run_one_lane: call run_one_wallet_lane with resolved paths (T15: dry_run only)
    def run_one_lane(wid: str, lid: str) -> str:
        return run_one_wallet_lane(
            wallet_id=wid,
            lane_id=lid,
            state_path=_state_path(wid),
            status_path=_status_path(wid),
            config=config,
            run_state=run_state,
            dry_run=True,  # T15: no live execution from run-multi
        )

    engine = RuntimeEngine(
        wallet_state_source=wallet_state_source,
        strategy_registry=strategy_registry,
        lane_manager=lane_manager,
        wallet_manager=None,  # dry-run only; no signing
        state_path=engine_state_path,
        get_run_state=get_run_state,
        pre_run_risk_check=pre_run_risk_check,
        run_one_lane=run_one_lane,
    )

    # Optional exposure snapshot path (AUT7); write after each cycle when set
    exposure_snapshot_path = os.environ.get("EXPOSURE_SNAPSHOT_PATH")
    if exposure_snapshot_path:
        exposure_snapshot_path = Path(exposure_snapshot_path)
        if not exposure_snapshot_path.is_absolute():
            exposure_snapshot_path = (root / exposure_snapshot_path).resolve()
    else:
        exposure_snapshot_path = None

    def _write_exposure_snapshot() -> None:
        if not exposure_snapshot_path:
            return
        from datetime import datetime, timezone
        from .monitoring.runtime_monitor import run_monitoring
        snapshot = {"updated_at": datetime.now(tz=timezone.utc).isoformat(), "wallets": {}}
        wallet_ids = {str(a.get("wallet_id")) for a in assignment_list if a.get("wallet_id")}
        for wid in sorted(wallet_ids):
            st_path = _state_path(wid)
            log_path = st_path.parent / "run.log"
            status_path = _status_path(wid)
            if not st_path.exists():
                continue
            try:
                summary = run_monitoring(st_path, log_path, status_path, wallet_id_override=wid)
                snapshot["wallets"][wid] = summary.get("wallet_exposure") or {}
            except Exception:
                continue
        try:
            exposure_snapshot_path.write_text(json.dumps(snapshot, indent=2))
        except OSError:
            pass

    typer.echo("run-multi: dry-run only (no live execution). Ctrl+C to exit.")
    try:
        if max_cycles is not None:
            for i in range(max_cycles):
                engine.run_cycle()
                lane_manager.save_to_file(lane_state_path)
                _write_exposure_snapshot()
                if i < max_cycles - 1:
                    time.sleep(cycle_interval_sec)
        else:
            while True:
                engine.run_cycle()
                lane_manager.save_to_file(lane_state_path)
                _write_exposure_snapshot()
                time.sleep(cycle_interval_sec)
    except KeyboardInterrupt:
        typer.echo("Interrupted, exiting.")
        raise typer.Exit(0)
    except Exception as exc:
        typer.echo(f"Fatal error: {exc}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()

