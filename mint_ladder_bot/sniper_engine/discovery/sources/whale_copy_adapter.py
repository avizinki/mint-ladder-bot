"""
Whale copy discovery source adapter.

Detects token buy transactions from a configured list of tracked smart/profitable wallets
and emits those mints as discovery candidates.

Safety mitigations:
1. Internal poll interval (DISCOVERY_WHALE_POLL_INTERVAL_S, default 60s):
   - Adapter caches results between polls; returns cached candidates if interval has not elapsed.
   - Prevents N * wallets RPC calls per 30s discovery cycle.
2. Wallet list cap (DISCOVERY_WHALE_MAX_WALLETS, default 20):
   - Caps number of wallets polled per cycle to prevent unbounded RPC load.
   - Logs warning if configured list exceeds cap.
3. All RPC calls inside try/except; returns [] on any failure.
4. No direct execution path — all candidates are returned to the pipeline as DiscoveredCandidate
   objects subject to the full filter → enrich → score → review_only gate chain.

source_id: "whale_copy"
source_confidence: per-wallet configurable (default 0.7)

Wallet config format (JSON file at DISCOVERY_WHALE_WALLETS_PATH):
  [
    {"address": "WalletAddr...", "label": "whale_1", "confidence": 0.8},
    {"address": "WalletAddr2...", "label": "whale_2"}
  ]

Or as comma-separated addresses via DISCOVERY_WHALE_WALLETS env var.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..model import DiscoveredCandidate, make_candidate
from ..registry import register_source

logger = logging.getLogger(__name__)

SOURCE_ID = "whale_copy"
DEFAULT_SOURCE_CONFIDENCE = 0.7
DEFAULT_POLL_INTERVAL_S = 60.0
DEFAULT_MAX_WALLETS = 20
DEFAULT_TX_LOOKBACK = 10  # max recent txs to inspect per wallet per poll

# Module-level state for poll-interval caching (mitigation #1)
_last_poll_time: float = 0.0
_cached_candidates: List[DiscoveredCandidate] = []


def _load_wallet_configs() -> List[Dict[str, Any]]:
    """
    Load wallet list from DISCOVERY_WHALE_WALLETS_PATH or DISCOVERY_WHALE_WALLETS env.
    Returns list of dicts with keys: address, label, confidence.
    """
    path_str = (os.getenv("DISCOVERY_WHALE_WALLETS_PATH") or "").strip()
    if path_str:
        path = Path(path_str)
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                if isinstance(raw, list):
                    configs = []
                    for entry in raw:
                        if isinstance(entry, str):
                            configs.append({"address": entry.strip(), "label": entry[:8], "confidence": DEFAULT_SOURCE_CONFIDENCE})
                        elif isinstance(entry, dict) and entry.get("address"):
                            configs.append({
                                "address": entry["address"].strip(),
                                "label": entry.get("label") or entry["address"][:8],
                                "confidence": float(entry.get("confidence", DEFAULT_SOURCE_CONFIDENCE)),
                            })
                    return configs
            except Exception as e:
                logger.warning("whale_copy: failed to load wallets from %s: %s", path_str, e)

    env_wallets = (os.getenv("DISCOVERY_WHALE_WALLETS") or "").strip()
    if env_wallets:
        configs = []
        for addr in env_wallets.split(","):
            addr = addr.strip()
            if addr:
                configs.append({"address": addr, "label": addr[:8], "confidence": DEFAULT_SOURCE_CONFIDENCE})
        return configs

    return []


def _fetch_wallet_buys(wallet_config: Dict[str, Any], tx_lookback: int) -> List[DiscoveredCandidate]:
    """
    Fetch recent token buy transactions for one wallet.
    Returns list of DiscoveredCandidate for each unique purchased mint.
    Returns [] on any failure.
    """
    address = wallet_config["address"]
    label = wallet_config["label"]
    confidence = wallet_config["confidence"]

    try:
        from solders.pubkey import Pubkey  # type: ignore
        from solana.rpc.api import Client  # type: ignore

        rpc_url = (os.getenv("RPC_ENDPOINT") or "").strip()
        if not rpc_url:
            return []

        client = Client(rpc_url, timeout=5)
        pubkey = Pubkey.from_string(address)

        # Fetch recent signatures
        sigs_response = client.get_signatures_for_address(pubkey, limit=tx_lookback)
        if not sigs_response or not hasattr(sigs_response, "value"):
            return []

        candidates: List[DiscoveredCandidate] = []
        seen_mints: set = set()

        for sig_info in sigs_response.value:
            if sig_info.err:
                continue  # skip failed txs
            sig = str(sig_info.signature)

            try:
                tx_response = client.get_transaction(
                    sig,
                    encoding="jsonParsed",
                    max_supported_transaction_version=0,
                )
                if not tx_response or not hasattr(tx_response, "value") or not tx_response.value:
                    continue

                mint = _extract_buy_mint(tx_response.value, address)
                if mint and mint not in seen_mints:
                    seen_mints.add(mint)
                    buy_sol = _extract_buy_sol(tx_response.value, address)
                    candidates.append(make_candidate(
                        mint=mint,
                        source_id=SOURCE_ID,
                        source_confidence=confidence,
                        discovery_signals={
                            "trigger_wallet": address,
                            "trigger_tx": sig,
                            "wallet_label": label,
                            "wallet_confidence": confidence,
                            "buy_amount_sol": buy_sol,
                        },
                    ))
            except Exception as e:
                logger.debug("whale_copy: tx parse failed sig=%s err=%s", sig[:16], str(e)[:100])
                continue

        return candidates

    except ImportError:
        logger.debug("whale_copy: solana/solders not available, returning []")
        return []
    except Exception as e:
        logger.warning("whale_copy: wallet fetch failed addr=%s err=%s", address[:12], str(e)[:150])
        return []


def _extract_buy_mint(tx_value: Any, wallet_address: str) -> Optional[str]:
    """
    Parse transaction to find the token mint that the wallet received/bought.
    Returns mint address string or None if not a token buy or can't be parsed.
    """
    try:
        # Try jsonParsed structure: look for token balance changes where wallet gained tokens
        meta = getattr(tx_value, "meta", None) or {}
        if hasattr(meta, "__dict__"):
            meta = meta.__dict__

        post_balances = meta.get("postTokenBalances") or []
        pre_balances = meta.get("preTokenBalances") or []

        pre_map: Dict[str, float] = {}
        for pb in pre_balances:
            if not hasattr(pb, "__dict__"):
                pb = pb if isinstance(pb, dict) else {}
            else:
                pb = pb.__dict__
            owner = (pb.get("owner") or "")
            mint = (pb.get("mint") or "")
            amount_str = ""
            ui = pb.get("uiTokenAmount") or {}
            if hasattr(ui, "__dict__"):
                ui = ui.__dict__
            amount_str = str(ui.get("uiAmountString") or "0")
            if owner == wallet_address and mint:
                try:
                    pre_map[mint] = float(amount_str)
                except ValueError:
                    pre_map[mint] = 0.0

        for pb in post_balances:
            if not hasattr(pb, "__dict__"):
                pb = pb if isinstance(pb, dict) else {}
            else:
                pb = pb.__dict__
            owner = (pb.get("owner") or "")
            mint = (pb.get("mint") or "")
            ui = pb.get("uiTokenAmount") or {}
            if hasattr(ui, "__dict__"):
                ui = ui.__dict__
            try:
                post_amount = float(str(ui.get("uiAmountString") or "0"))
            except ValueError:
                post_amount = 0.0
            pre_amount = pre_map.get(mint, 0.0)
            if owner == wallet_address and mint and post_amount > pre_amount:
                return mint

        return None
    except Exception:
        return None


def _extract_buy_sol(tx_value: Any, wallet_address: str) -> Optional[float]:
    """
    Estimate SOL spent in a buy transaction. Returns None if can't be determined.
    """
    try:
        meta = getattr(tx_value, "meta", None) or {}
        if hasattr(meta, "__dict__"):
            meta = meta.__dict__

        pre_sols = meta.get("preBalances") or []
        post_sols = meta.get("postBalances") or []

        # Look at account keys to find wallet index
        tx = getattr(tx_value, "transaction", None)
        if tx is None:
            return None
        msg = getattr(tx, "message", None)
        if msg is None:
            return None
        account_keys = getattr(msg, "accountKeys", None) or []

        for i, key in enumerate(account_keys):
            key_str = str(key)
            if key_str == wallet_address:
                if i < len(pre_sols) and i < len(post_sols):
                    diff_lamports = pre_sols[i] - post_sols[i]
                    if diff_lamports > 0:
                        return round(diff_lamports / 1e9, 6)
                break
        return None
    except Exception:
        return None


def _fetch(limit: int) -> List[DiscoveredCandidate]:
    """
    Main adapter fetch function. Called by registry each discovery cycle.

    Mitigation #1: Returns cached results if poll interval has not elapsed.
    Mitigation #1: Caps wallet list to DEFAULT_MAX_WALLETS.
    Returns [] if no wallets configured or all fetches fail.
    """
    global _last_poll_time, _cached_candidates

    poll_interval = float(os.getenv("DISCOVERY_WHALE_POLL_INTERVAL_S", str(DEFAULT_POLL_INTERVAL_S)))
    max_wallets = int(os.getenv("DISCOVERY_WHALE_MAX_WALLETS", str(DEFAULT_MAX_WALLETS)))

    now = time.monotonic()
    if now - _last_poll_time < poll_interval:
        # Return cached results — interval not elapsed
        logger.debug("whale_copy: returning %d cached candidates (interval not elapsed)", len(_cached_candidates))
        return list(_cached_candidates)[:limit]

    wallet_configs = _load_wallet_configs()
    if not wallet_configs:
        return []

    # Cap wallet list (mitigation #1)
    if len(wallet_configs) > max_wallets:
        logger.warning(
            "whale_copy: wallet list has %d entries, capping to %d (DISCOVERY_WHALE_MAX_WALLETS)",
            len(wallet_configs), max_wallets,
        )
        wallet_configs = wallet_configs[:max_wallets]

    tx_lookback = int(os.getenv("DISCOVERY_WHALE_TX_LOOKBACK", str(DEFAULT_TX_LOOKBACK)))
    all_candidates: List[DiscoveredCandidate] = []
    seen_mints: set = set()

    for wc in wallet_configs:
        try:
            wallet_candidates = _fetch_wallet_buys(wc, tx_lookback)
            for c in wallet_candidates:
                if c.mint not in seen_mints:
                    seen_mints.add(c.mint)
                    all_candidates.append(c)
        except Exception as e:
            logger.warning("whale_copy: error fetching wallet %s: %s", wc.get("address", "?")[:12], str(e)[:100])

    _last_poll_time = time.monotonic()
    _cached_candidates = list(all_candidates)

    logger.info("whale_copy: polled %d wallets, found %d unique mint candidates", len(wallet_configs), len(all_candidates))
    return all_candidates[:limit]


def register() -> None:
    register_source(SOURCE_ID, _fetch)
