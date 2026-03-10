from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import httpx

from .config import Config
from .models import (
    DexscreenerMarketInfo,
    DexscreenerTxns24h,
    EntryInfo,
    MarketInfo,
    MintStatus,
    RpcInfo,
    SolBalance,
    StatusFile,
)
from .jupiter import get_token_metadata_batch
from .rpc import RpcClient
from .state import RuntimeState
from .tx_infer import infer_entries_for_mints

logger = logging.getLogger(__name__)

_ENTRY_MIN, _ENTRY_MAX = 1e-12, 1e3


def sync_entry_from_state_to_status(status_data: StatusFile, state: RuntimeState) -> None:
    """Refresh each status mint's entry from state so status reflects state's tx-derived entry and tx signature."""
    from .models import EntryInfo

    status_by_mint = {m.mint: m for m in status_data.mints}
    for mint_addr, ms in state.mints.items():
        sm = status_by_mint.get(mint_addr)
        if sm is None:
            continue
        ep = getattr(ms, "entry_price_sol_per_token", 0) or 0
        if ep < _ENTRY_MIN or ep > _ENTRY_MAX:
            if getattr(ms, "lots", None):
                for lot in ms.lots:
                    if getattr(lot, "source", None) in ("tx_exact", "tx_parsed"):
                        lp = getattr(lot, "entry_price_sol_per_token", None)
                        if lp is not None and _ENTRY_MIN <= lp <= _ENTRY_MAX:
                            ep = lp
                            break
        if ep < _ENTRY_MIN or ep > _ENTRY_MAX:
            continue
        src = getattr(ms, "entry_source", None) or "market_bootstrap"
        if src in ("tx_exact", "tx_parsed"):
            src = "inferred_from_tx"
        entry_source = src if src in ("user", "inferred_from_tx", "bootstrap_buy", "market_bootstrap") else "market_bootstrap"
        tx_sig = getattr(ms, "entry_tx_signature", None)
        if not tx_sig and getattr(ms, "lots", None):
            for lot in ms.lots:
                if getattr(lot, "source", None) in ("tx_exact", "tx_parsed") and getattr(lot, "tx_signature", None):
                    tx_sig = lot.tx_signature
                    break
        sm.entry = EntryInfo(
            entry_price_sol_per_token=ep,
            entry_source=entry_source,
            entry_tx_signature=tx_sig,
            mode=getattr(sm.entry, "mode", "auto") or "auto",
        )


def write_status_synced(status_data: StatusFile, state: RuntimeState, path: Path) -> None:
    """
    Sync entry (and state-derived display truth) from state into status, then write status.json.
    Use whenever status is persisted so status never knows less than state for truthful display.
    """
    sync_entry_from_state_to_status(status_data, state)
    path.write_text(status_data.model_dump_json(indent=2))


def _atomic_write_json(path: Path, data: Dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp_path, path)


def _fetch_dexscreener_for_mint(
    client: httpx.Client, mint: str
) -> Tuple[Optional[DexscreenerMarketInfo], Optional[str], Optional[str]]:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        resp = client.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("DexScreener request failed for %s: %s", mint, exc)
        return (None, None, None)

    pairs: List[dict] = data.get("pairs") or []
    if not pairs:
        return (None, None, None)

    # Prefer SOL/WSOL quote pairs with highest liquidity; otherwise, highest liquidity overall.
    def _liquidity_usd(p: dict) -> float:
        liq = p.get("liquidity") or {}
        usd = liq.get("usd")
        try:
            return float(usd)
        except (TypeError, ValueError):
            return 0.0

    sol_like = []
    others = []
    for p in pairs:
        base = (p.get("baseToken") or {}).get("address")
        quote_sym = (p.get("quoteToken") or {}).get("symbol", "").upper()
        if base == mint and quote_sym in ("SOL", "WSOL"):
            sol_like.append(p)
        else:
            others.append(p)

    candidates = sol_like or others
    if not candidates:
        return (None, None, None)

    main = max(candidates, key=_liquidity_usd)
    base_token = main.get("baseToken") or {}
    ds_symbol = (base_token.get("symbol") or "").strip() or None
    ds_name = (base_token.get("name") or "").strip() or None
    if base_token.get("address") != mint:
        ds_symbol = None
        ds_name = None

    liq = main.get("liquidity") or {}
    vol = main.get("volume") or {}
    txns = main.get("txns") or {}
    h24 = txns.get("h24") or {}

    try:
        liquidity_usd = float(liq.get("usd")) if liq.get("usd") is not None else None
    except (TypeError, ValueError):
        liquidity_usd = None

    try:
        price_usd = float(main.get("priceUsd")) if main.get("priceUsd") is not None else None
    except (TypeError, ValueError):
        price_usd = None

    # DexScreener exposes priceNative as string for SOL pairs.
    try:
        price_native = float(main.get("priceNative")) if main.get("priceNative") is not None else None
    except (TypeError, ValueError):
        price_native = None

    try:
        volume24h_usd = float(vol.get("h24")) if vol.get("h24") is not None else None
    except (TypeError, ValueError):
        volume24h_usd = None

    buys = h24.get("buys")
    sells = h24.get("sells")
    try:
        buys_i = int(buys) if buys is not None else None
    except (TypeError, ValueError):
        buys_i = None
    try:
        sells_i = int(sells) if sells is not None else None
    except (TypeError, ValueError):
        sells_i = None

    ds_info = DexscreenerMarketInfo(
        pair_address=main.get("pairAddress"),
        dex_id=main.get("dexId"),
        liquidity_usd=liquidity_usd,
        price_usd=price_usd,
        price_native=price_native,
        volume24h_usd=volume24h_usd,
        txns24h=DexscreenerTxns24h(buys=buys_i, sells=sells_i),
    )
    if base_token.get("address") != mint:
        ds_symbol = None
        ds_name = None
    return (ds_info, ds_symbol, ds_name)


def discover_new_mints(
    wallet_pubkey: str,
    rpc: "RpcClient",
    config: Config,
    existing_mint_set: Set[str],
) -> List[MintStatus]:
    """
    Fetch wallet token accounts and return MintStatus list for mints not in existing_mint_set,
    with balance >= min_buy_detection_raw and liquidity >= min_liquidity_usd_for_track (when available).
    Used by runner for live validation: auto-detect newly purchased tokens.
    """
    min_raw = getattr(config, "min_buy_detection_raw", 10_000) or 10_000
    # Discovery threshold: add new mints even with low/unknown liquidity; trading guard still uses min_liquidity_usd_for_track
    min_liq = getattr(config, "discover_min_liquidity_usd", 0.0)
    if min_liq is None:
        min_liq = 0.0
    token_accounts: List[dict] = []
    for program_id in (
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    ):
        try:
            token_accounts.extend(rpc.get_token_accounts_by_owner(wallet_pubkey, program_id=program_id))
        except Exception as exc:
            logger.debug("discover_new_mints: get_token_accounts_by_owner failed for %s: %s", program_id, exc)
    parsed: List[Tuple[str, str, int, str, float]] = []
    for item in token_accounts:
        try:
            pubkey = item.get("pubkey")
            info = (item.get("account") or {}).get("data", {}).get("parsed", {}).get("info") or {}
            token_amount = info.get("tokenAmount") or {}
            amount_raw = token_amount.get("amount")
            decimals = int(token_amount.get("decimals") or 0)
            ui_amount = float(token_amount.get("uiAmount") or 0.0)
            mint_addr = info.get("mint")
            if not mint_addr or amount_raw is None or ui_amount <= 0:
                continue
            if mint_addr in existing_mint_set:
                continue
            if int(amount_raw) < min_raw:
                logger.debug("BUY_DETECTION_SKIPPED mint=%s reason=dust amount_raw=%s min_raw=%s", mint_addr[:12], amount_raw, min_raw)
                continue
            parsed.append((mint_addr, pubkey, decimals, amount_raw, ui_amount))
        except Exception as exc:
            logger.debug("discover_new_mints: parse item failed: %s", exc)
    if not parsed:
        return []
    new_mints: List[MintStatus] = []
    with httpx.Client(timeout=10.0) as http_client:
        for mint_addr, pubkey, decimals, amount_raw, ui_amount in parsed:
            ds_info, ds_symbol, ds_name = _fetch_dexscreener_for_mint(http_client, mint_addr)
            liq = (ds_info.liquidity_usd if ds_info else None) or None
            if min_liq > 0 and liq is not None and liq < min_liq:
                logger.info("BUY_DETECTION_SKIPPED mint=%s reason=liquidity liquidity_usd=%.0f threshold=%.0f", mint_addr[:12], liq, min_liq)
                continue
            market = MarketInfo(dexscreener=ds_info) if ds_info else MarketInfo()
            entry = EntryInfo()
            if ds_info and ds_info.price_native is not None and float(ds_info.price_native) > 0:
                entry = EntryInfo(
                    entry_price_sol_per_token=float(ds_info.price_native),
                    entry_source="market_bootstrap",
                )
            new_mints.append(
                MintStatus(
                    mint=mint_addr,
                    token_account=pubkey,
                    decimals=decimals,
                    balance_ui=ui_amount,
                    balance_raw=str(amount_raw),
                    symbol=ds_symbol,
                    name=ds_name,
                    entry=entry,
                    market=market,
                )
            )
    return new_mints


def build_status_snapshot(wallet_pubkey: str, out_path: Path, config: Config) -> None:
    """
    Build a single status.json snapshot for the given wallet and write it atomically.
    """

    rpc = RpcClient(
        config.rpc_endpoint,
        timeout_s=config.rpc_timeout_s,
        max_retries=config.max_retries,
    )
    try:
        # If a previous status file exists, load it so we can preserve any
        # user-specified entry information across snapshots.
        prev_entries: Dict[str, EntryInfo] = {}
        if out_path.exists():
            try:
                prev_data = json.loads(out_path.read_text())
                prev_status = StatusFile.model_validate(prev_data)
                for m in prev_status.mints:
                    prev_entries[m.mint] = m.entry
            except Exception as exc:  # best-effort only
                logger.debug("Failed to load previous status file: %s", exc)

        latency_ms = rpc.measure_latency_ms()
        lamports = rpc.get_balance(wallet_pubkey)
        sol = lamports / 1e9

        # Fetch token accounts from both the classic SPL Token program and
        # Token-2022 so mints like WARBROS / bǐngwǔ are included.
        token_accounts: List[dict] = []
        token_programs = [
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
        ]
        for program_id in token_programs:
            try:
                token_accounts.extend(
                    rpc.get_token_accounts_by_owner(wallet_pubkey, program_id=program_id)
                )
            except Exception as exc:
                # Best-effort: log and continue so one bad program ID doesn't
                # break the whole snapshot.
                logger.debug(
                    "Failed to fetch token accounts for program %s: %s",
                    program_id,
                    exc,
                )

        # Parse token accounts into a list of (mint, pubkey, decimals, amount_raw, ui_amount).
        parsed_items: List[Tuple[str, str, int, str, float]] = []
        for item in token_accounts:
            try:
                pubkey = item.get("pubkey")
                account = item.get("account") or {}
                data = account.get("data") or {}
                parsed = data.get("parsed") or {}
                info = parsed.get("info") or {}
                token_amount = info.get("tokenAmount") or {}
                amount_raw = token_amount.get("amount")
                decimals = int(token_amount.get("decimals") or 0)
                ui_amount = token_amount.get("uiAmount")
                if amount_raw is None:
                    continue
                if float(ui_amount or 0.0) <= 0.0:
                    continue
                mint_addr = info.get("mint")
                if not mint_addr:
                    continue
                parsed_items.append((mint_addr, pubkey, decimals, amount_raw, float(ui_amount)))
            except Exception as exc:
                logger.debug("Failed to parse token account: %s", exc)
                continue

        # Fetch symbol/name for all mints from Jupiter (same API key as swap).
        all_mints = list({m[0] for m in parsed_items})
        jupiter_meta = get_token_metadata_batch(config, all_mints)

        mints: List[MintStatus] = []
        with httpx.Client(timeout=10.0) as http_client:
            for mint_addr, pubkey, decimals, amount_raw, ui_amount in parsed_items:
                # DexScreener enrichment (best-effort); also returns symbol/name from pair baseToken.
                ds_info, ds_symbol, ds_name = _fetch_dexscreener_for_mint(http_client, mint_addr)
                symbol: Optional[str] = None
                name: Optional[str] = None
                jup = jupiter_meta.get(mint_addr)
                if jup and (jup[0] or jup[1]):
                    symbol, name = jup[0], jup[1]
                if (symbol is None or name is None) and (ds_symbol or ds_name):
                    symbol = symbol or ds_symbol
                    name = name or ds_name

                if ds_info is not None:
                    market = MarketInfo(dexscreener=ds_info)
                else:
                    market = MarketInfo()

                prev_entry = prev_entries.get(mint_addr)
                entry = prev_entry if prev_entry is not None else EntryInfo()

                mint_status = MintStatus(
                    mint=mint_addr,
                    token_account=pubkey,
                    decimals=decimals,
                    balance_ui=ui_amount,
                    balance_raw=str(amount_raw),
                    symbol=symbol,
                    name=name,
                    entry=entry,
                    market=market,
                )
                mints.append(mint_status)

        # Entry price inference from recent transactions (best-effort, bounded).
        # Only attempt inference for mints that do not already have a
        # user-provided entry (mode=manual or entry_source=user).
        if mints and config.entry_infer_signature_limit > 0:
            limit = max(0, config.entry_infer_signature_limit)
            signatures = rpc.get_signatures_for_address(wallet_pubkey, limit=limit)
            mint_addrs = [
                m.mint
                for m in mints
                if not (
                    m.entry.mode == "manual" or m.entry.entry_source == "user"
                )
            ]
            if mint_addrs:
                decimals_by_mint = {m.mint: m.decimals for m in mints}
                inferred_entries = infer_entries_for_mints(
                    wallet_pubkey=wallet_pubkey,
                    mints=mint_addrs,
                    signatures=signatures,
                    rpc=rpc,
                    decimals_by_mint=decimals_by_mint,
                )
                by_mint: Dict[str, MintStatus] = {m.mint: m for m in mints}
                for mint_addr, entry in inferred_entries.items():
                    ms = by_mint.get(mint_addr)
                    if ms is not None:
                        ms.entry = entry

        # Backfill entry from state.json when present, so re-running status doesn't wipe known entries.
        state_path = out_path.parent / "state.json"
        if state_path.exists():
            try:
                from .state import load_state
                state = load_state(state_path, out_path)
                _ENTRY_MIN, _ENTRY_MAX = 1e-12, 1e3
                for m in mints:
                    ms = state.mints.get(m.mint)
                    if ms is None:
                        continue
                    ep = getattr(ms, "entry_price_sol_per_token", 0) or 0
                    # Prefer lot-derived tx-exact/tx-parsed entries when present and sane.
                    if getattr(ms, "lots", None):
                        for lot in ms.lots:
                            if getattr(lot, "source", None) in ("tx_exact", "tx_parsed"):
                                lp = getattr(lot, "entry_price_sol_per_token", None)
                                if lp is not None and _ENTRY_MIN <= lp <= _ENTRY_MAX:
                                    ep = lp
                                    break
                    # Override when ep is in sane band and either current entry is unknown/zero
                    # or clearly inconsistent with the lot-derived entry (e.g. wrong scale).
                    current_ep = m.entry.entry_price_sol_per_token or 0.0
                    if _ENTRY_MIN <= ep <= _ENTRY_MAX and (
                        current_ep <= 0
                        or m.entry.entry_source == "unknown"
                        or not (_ENTRY_MIN <= current_ep <= _ENTRY_MAX)
                        or abs(ep - current_ep) / ep > 1e-3  # >0.1% relative difference
                    ):
                        src = getattr(ms, "entry_source", None) or "market_bootstrap"
                        if src in ("tx_exact", "tx_parsed"):
                            src = "inferred_from_tx"
                        entry_source = src if src in ("user", "inferred_from_tx", "bootstrap_buy", "market_bootstrap") else "market_bootstrap"
                        tx_sig = getattr(ms, "entry_tx_signature", None)
                        if not tx_sig and getattr(ms, "lots", None):
                            for lot in ms.lots:
                                if getattr(lot, "source", None) in ("tx_exact", "tx_parsed") and getattr(lot, "tx_signature", None):
                                    tx_sig = lot.tx_signature
                                    break
                        m.entry = EntryInfo(
                            entry_price_sol_per_token=ep,
                            entry_source=entry_source,
                            entry_tx_signature=tx_sig,
                            mode="auto",
                        )
            except Exception as exc:
                logger.debug("Failed to backfill entry from state.json: %s", exc)

        # Do NOT synthesize entries for unknown mints (no price_native/1.10, no 1e-15).
        # Unknown entry remains entry_price_sol_per_token=0, entry_source="unknown";
        # runner skips such mints. Manual/user entries were preserved above via prev_entries.

        status_file = StatusFile(
            version=1,
            created_at=datetime.now(tz=timezone.utc),
            wallet=wallet_pubkey,
            rpc=RpcInfo(endpoint=config.rpc_endpoint, latency_ms=latency_ms),
            sol=SolBalance(lamports=lamports, sol=sol),
            mints=mints,
        )

        _atomic_write_json(out_path, json.loads(status_file.model_dump_json()))
        logger.info("Wrote status snapshot to %s", out_path)
    finally:
        rpc.close()

