#!/usr/bin/env python3
"""
Deep source-wallet provenance reconstruction (read-only).

For transfers from a fixed source wallet into the trading wallet, this tool:

- Reads source_wallet_provenance_report.json to get transfer rows (mint, amount, sig, time).
- For each mint, scans source-wallet history around the transfer to find acquisition
  candidates using:
  1) explicit swap detection (existing tx parser),
  2) inferred swap from token delta + SOL delta for the source wallet,
  3) fallback pool price from Dexscreener when both fail.
- Produces a deep provenance report with per-mint best acquisition choice and
  a ranked list of top candidates.

Constraints:
- Analysis-only; does NOT mutate state.json or status.json.
- Uses scratch reconstruction and RPC history only.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_env() -> None:
    for p in (_REPO / ".env", Path(".env")):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()


_load_env()

TRADING_WALLET = os.environ.get(
    "TRADING_WALLET",
    "3LEZBhZiBjmaFN4uwZvncoS3MvDq4cPhSCgMjH3vS5HR",
)
SOURCE_WALLET = os.environ.get(
    "SOURCE_WALLET",
    "9T6wvKnUiQDctcE8DyN8kfMxQcchqJzFQNiXfvYvU1fY",
)

DATA_DIR = _REPO / "runtime" / "projects" / "mint_ladder_bot"
BASE_PROVENANCE_JSON = DATA_DIR / "source_wallet_provenance_report.json"
JSON_REPORT_PATH = DATA_DIR / "source_wallet_provenance_deep_report.json"
MD_REPORT_PATH = DATA_DIR / "source_wallet_provenance_deep_report.md"


def _dexscreener_price_native(http_client: Any, mint: str) -> Optional[float]:
    """
    Best-effort Dexscreener price_native fetch (current pool price).
    Returns SOL per token (float) or None.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        resp = http_client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        best = None
        best_liq = -1.0
        for p in pairs:
            liq = (p.get("liquidity") or {}).get("usd")
            try:
                liq_f = float(liq) if liq is not None else 0.0
            except (ValueError, TypeError):
                liq_f = 0.0
            if liq_f > best_liq:
                best_liq = liq_f
                best = p
        if not best:
            return None
        price_native = best.get("priceNative")
        if price_native is None:
            return None
        return float(price_native)
    except Exception:
        return None


def _sol_delta_for_wallet(tx: Dict[str, Any], wallet: str) -> Optional[int]:
    """
    Compute SOL delta (lamports) for `wallet` in a transaction, based on
    preBalances/postBalances and accountKeys. Returns post - pre (can be negative)
    or None if wallet is not in accountKeys.
    """
    message = (tx.get("transaction") or {}).get("message") or {}
    account_keys = message.get("accountKeys") or []
    try:
        idx = account_keys.index(wallet)
    except ValueError:
        return None
    meta = tx.get("meta") or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return None
    try:
        pre_lamports = int(pre[idx])
        post_lamports = int(post[idx])
    except (ValueError, TypeError):
        return None
    return post_lamports - pre_lamports


def _token_delta_for_wallet_and_mint(
    tx: Dict[str, Any],
    wallet: str,
    mint: str,
) -> Optional[int]:
    """
    Compute raw token delta (post - pre) for (wallet, mint) in a transaction.
    """
    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    pre_raw = 0
    post_raw = 0
    for e in pre:
        if e.get("owner") != wallet:
            continue
        if e.get("mint") != mint:
            continue
        ui = e.get("uiTokenAmount") or {}
        amt = ui.get("amount")
        if amt is None:
            continue
        try:
            pre_raw = int(amt)
        except (ValueError, TypeError):
            continue
    for e in post:
        if e.get("owner") != wallet:
            continue
        if e.get("mint") != mint:
            continue
        ui = e.get("uiTokenAmount") or {}
        amt = ui.get("amount")
        if amt is None:
            continue
        try:
            post_raw = int(amt)
        except (ValueError, TypeError):
            continue
    return post_raw - pre_raw


@dataclass
class AcquisitionCandidate:
    mint: str
    symbol: Optional[str]
    acquisition_sig: str
    acquisition_slot: Optional[int]
    acquisition_time: Optional[str]
    method: str  # explicit_swap | inferred_swap_from_sol_delta | fallback_pool_price | unknown
    token_delta_raw: Optional[int]
    sol_delta_lamports: Optional[int]
    entry_price_estimated: Optional[float]  # SOL per token
    confidence_score: str  # HIGH_CONFIDENCE | MEDIUM_CONFIDENCE | LOW_CONFIDENCE
    confidence_reason: str
    distance_slots: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PerMintDeepProvenance:
    mint: str
    symbol: Optional[str]
    transfer_sig: str
    transfer_time: Optional[str]
    amount_transferred_raw: int
    best_source_acquisition_sig: Optional[str]
    acquisition_time: Optional[str]
    acquisition_method: str
    token_delta_raw: Optional[int]
    sol_delta: Optional[float]
    entry_price_estimated: Optional[float]
    confidence_score: str
    confidence_reason: str
    candidates: List[AcquisitionCandidate]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert lamports to SOL for candidates sol_delta if needed in callers.
        return d


def main() -> int:
    from httpx import Client as HttpClient

    from mint_ladder_bot.config import Config
    from mint_ladder_bot.rpc import RpcClient
    from mint_ladder_bot.transfer_provenance_analysis import _get_block_time
    from mint_ladder_bot.tx_lot_engine import _parse_buy_events_from_tx

    if not BASE_PROVENANCE_JSON.exists():
        print(
            f"Base provenance report not found at {BASE_PROVENANCE_JSON}",
            file=sys.stderr,
        )
        return 1

    base = json.loads(BASE_PROVENANCE_JSON.read_text())
    rows = base.get("rows") or []

    # Group transfers by mint (current report has one per mint, but keep general).
    transfers_by_mint: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        mint = r.get("mint")
        if not mint:
            continue
        transfers_by_mint.setdefault(mint, []).append(r)

    config = Config()
    rpc = RpcClient(config.rpc_endpoint, timeout_s=config.rpc_timeout_s)
    http_client = HttpClient()

    # Fetch source wallet signatures once (bounded).
    max_sigs = getattr(config, "reconstruction_max_signatures_per_wallet", 500)
    try:
        sig_infos = rpc.get_signatures_for_address(SOURCE_WALLET, limit=max_sigs)
    except Exception as exc:
        print(f"get_signatures_for_address failed: {exc}", file=sys.stderr)
        return 1

    # Normalize: each item should have signature and slot.
    all_source_sigs: List[Dict[str, Any]] = []
    for s in sig_infos or []:
        if not isinstance(s, dict):
            continue
        sig = s.get("signature")
        if not sig:
            continue
        all_source_sigs.append(
            {
                "signature": sig,
                "slot": s.get("slot"),
            }
        )

    deep_results: List[PerMintDeepProvenance] = []

    # Helper to get tx with cache.
    tx_cache: Dict[str, Dict[str, Any]] = {}

    def _get_tx(signature: str) -> Optional[Dict[str, Any]]:
        if signature in tx_cache:
            return tx_cache[signature]
        try:
            tx = rpc.get_transaction(signature)
        except Exception:
            tx = None
        if isinstance(tx, dict):
            tx_cache[signature] = tx
            return tx
        return None

    # Build simple decimals/symbol maps from base report (if present) – fallback to status as needed.
    symbol_by_mint: Dict[str, Optional[str]] = {}
    for r in rows:
        mint = r.get("mint")
        if not mint:
            continue
        if mint not in symbol_by_mint:
            symbol_by_mint[mint] = r.get("symbol")

    # For each mint, analyze deepest provenance.
    WINDOW_SLOTS = 50_000  # slot window before transfer to search for acquisition

    for mint, mint_transfers in transfers_by_mint.items():
        # Use the latest transfer for that mint (highest slot).
        latest = max(
            mint_transfers, key=lambda r: r.get("transfer_slot") or 0
        )
        transfer_sig = latest.get("transfer_sig")
        transfer_slot = latest.get("transfer_slot")
        transfer_time = latest.get("transfer_time")
        try:
            amount_transferred_raw = int(
                latest.get("amount_transferred_raw", 0) or 0
            )
        except (ValueError, TypeError):
            amount_transferred_raw = 0
        symbol = symbol_by_mint.get(mint)

        # Candidate acquisition txs: subset of source-wallet signatures before transfer_slot, within a window.
        candidate_sig_infos: List[Dict[str, Any]] = []
        for s in all_source_sigs:
            slot = s.get("slot")
            if transfer_slot is None or slot is None:
                continue
            if slot > transfer_slot:
                continue
            if slot < transfer_slot - WINDOW_SLOTS:
                continue
            candidate_sig_infos.append(s)

        # Sort newest→oldest within window.
        candidate_sig_infos.sort(
            key=lambda s: s.get("slot") or 0, reverse=True
        )

        candidates: List[AcquisitionCandidate] = []

        for s in candidate_sig_infos:
            sig = s.get("signature")
            slot = s.get("slot")
            if not sig:
                continue
            tx = _get_tx(sig)
            if not tx:
                continue

            bt = _get_block_time(tx)
            acq_time = bt.isoformat() if isinstance(bt, datetime) else None

            # 1) Explicit swap parsing for this mint and source wallet.
            buy_events = _parse_buy_events_from_tx(
                tx, SOURCE_WALLET, sig, {mint}, {}
            )
            mint_events = [e for e in buy_events if e.mint == mint]

            token_delta_raw = _token_delta_for_wallet_and_mint(
                tx, SOURCE_WALLET, mint
            )
            sol_delta_lamports = _sol_delta_for_wallet(tx, SOURCE_WALLET)
            entry_price_estimated: Optional[float] = None
            method = "unknown"
            confidence = "LOW_CONFIDENCE"
            reason = "no_price_signal"

            # Highest priority: explicit swap event with valid entry price.
            if mint_events:
                ev = mint_events[0]
                ep = getattr(ev, "entry_price_sol_per_token", None)
                if ep is not None and ep > 0:
                    entry_price_estimated = ep
                    method = "explicit_swap"
                    confidence = "HIGH_CONFIDENCE"
                    reason = "explicit_swap_event_with_entry_price"
                else:
                    method = "explicit_swap"
                    confidence = "MEDIUM_CONFIDENCE"
                    reason = "explicit_swap_without_entry_price"

            # Second priority: inferred swap from SOL delta + token delta.
            if entry_price_estimated is None and (
                token_delta_raw is not None and token_delta_raw > 0
            ):
                if sol_delta_lamports is not None and sol_delta_lamports < 0:
                    try:
                        token_human = token_delta_raw / (10 ** 6)
                        # We do not know decimals here; treat as 1e6 default.
                        # This still gives consistent relative prices across candidates.
                        sol_spent = abs(sol_delta_lamports) / 1e9
                        if token_human > 0 and sol_spent > 0:
                            entry_price_estimated = sol_spent / token_human
                            method = "inferred_swap_from_sol_delta"
                            # Higher confidence when token_delta close to transfer amount.
                            if (
                                amount_transferred_raw > 0
                                and abs(token_delta_raw - amount_transferred_raw)
                                / amount_transferred_raw
                                <= 0.25
                            ):
                                confidence = "HIGH_CONFIDENCE"
                                reason = "token_delta_and_sol_delta_match_transfer_within_25pct"
                            else:
                                confidence = "MEDIUM_CONFIDENCE"
                                reason = "token_delta_and_sol_delta_indicate_swap_but_not_exact_transfer_match"
                    except Exception:
                        pass

            # Record candidate even if we only have partial info.
            distance_slots = (
                transfer_slot - slot if transfer_slot is not None and slot is not None else None
            )
            cand = AcquisitionCandidate(
                mint=mint,
                symbol=symbol,
                acquisition_sig=sig,
                acquisition_slot=slot,
                acquisition_time=acq_time,
                method=method,
                token_delta_raw=token_delta_raw,
                sol_delta_lamports=sol_delta_lamports,
                entry_price_estimated=entry_price_estimated,
                confidence_score=confidence,
                confidence_reason=reason,
                distance_slots=distance_slots,
            )
            candidates.append(cand)

        # Ranking: explicit_swap > inferred_swap_from_sol_delta > others.
        def _priority(c: AcquisitionCandidate) -> Tuple[int, float, int]:
            if c.method == "explicit_swap":
                m = 0
            elif c.method == "inferred_swap_from_sol_delta":
                m = 1
            else:
                m = 2
            # Prefer token_delta close to transfer amount.
            if c.token_delta_raw is not None and amount_transferred_raw > 0:
                rel = abs(c.token_delta_raw - amount_transferred_raw) / amount_transferred_raw
            else:
                rel = 1.0
            dist = abs(c.distance_slots) if c.distance_slots is not None else 10**9
            return (m, rel, dist)

        candidates_sorted = sorted(candidates, key=_priority)
        best: Optional[AcquisitionCandidate] = None
        if candidates_sorted:
            # Only accept best if it has some price signal.
            for c in candidates_sorted:
                if c.entry_price_estimated is not None:
                    best = c
                    break

        # Fallback: use prior pool price estimate if no candidate had a price.
        if best is None:
            price_native = _dexscreener_price_native(http_client, mint)
            if price_native is not None:
                best = AcquisitionCandidate(
                    mint=mint,
                    symbol=symbol,
                    acquisition_sig=transfer_sig or "",
                    acquisition_slot=transfer_slot,
                    acquisition_time=transfer_time,
                    method="fallback_pool_price",
                    token_delta_raw=None,
                    sol_delta_lamports=None,
                    entry_price_estimated=price_native,
                    confidence_score="MEDIUM_CONFIDENCE",
                    confidence_reason="no_swap_or_inferred_candidate; using pool price",
                    distance_slots=0,
                )

        # If still none, synthesize a LOW_CONFIDENCE unknown.
        if best is None:
            best = AcquisitionCandidate(
                mint=mint,
                symbol=symbol,
                acquisition_sig=transfer_sig or "",
                acquisition_slot=transfer_slot,
                acquisition_time=transfer_time,
                method="unknown",
                token_delta_raw=None,
                sol_delta_lamports=None,
                entry_price_estimated=None,
                confidence_score="LOW_CONFIDENCE",
                confidence_reason="no convincing acquisition trace",
                distance_slots=0,
            )

        sol_delta_sol: Optional[float] = None
        if best.sol_delta_lamports is not None:
            sol_delta_sol = best.sol_delta_lamports / 1e9

        per_mint = PerMintDeepProvenance(
            mint=mint,
            symbol=symbol,
            transfer_sig=transfer_sig or "",
            transfer_time=transfer_time,
            amount_transferred_raw=amount_transferred_raw,
            best_source_acquisition_sig=best.acquisition_sig,
            acquisition_time=best.acquisition_time,
            acquisition_method=best.method,
            token_delta_raw=best.token_delta_raw,
            sol_delta=sol_delta_sol,
            entry_price_estimated=best.entry_price_estimated,
            confidence_score=best.confidence_score,
            confidence_reason=best.confidence_reason,
            candidates=candidates_sorted[:3],
        )
        deep_results.append(per_mint)

    rpc.close()
    http_client.close()

    json_report = {
        "destination_wallet": TRADING_WALLET,
        "source_wallet": SOURCE_WALLET,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "mints_analyzed": len(deep_results),
        "results": [r.to_dict() for r in deep_results],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT_PATH.write_text(json.dumps(json_report, indent=2), encoding="utf-8")

    # Markdown summary.
    lines: List[str] = []
    lines.append("# Deep source-wallet provenance report")
    lines.append("")
    lines.append(f"- Destination (trading) wallet: `{TRADING_WALLET}`")
    lines.append(f"- Source wallet: `{SOURCE_WALLET}`")
    lines.append(f"- Generated at: {json_report['generated_at']}")
    lines.append(f"- Mints analyzed: {len(deep_results)}")
    lines.append("")
    if deep_results:
        lines.append("## Per-mint summary")
        lines.append("")
        lines.append(
            "| Mint | Symbol | Transfer Sig | Best Acquisition Sig | Method | Entry Price (SOL) | Confidence |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- | --- |"
        )
        for r in deep_results:
            ep = r.entry_price_estimated if r.entry_price_estimated is not None else ""
            lines.append(
                f"| `{r.mint[:8]}…` | {r.symbol or ''} | `{r.transfer_sig[:12]}…` | "
                f"`{(r.best_source_acquisition_sig or '')[:12]}…` | {r.acquisition_method} | {ep} | {r.confidence_score} |"
            )
        lines.append("")

    MD_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"JSON deep report written to {JSON_REPORT_PATH}")
    print(f"Markdown deep report written to {MD_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

