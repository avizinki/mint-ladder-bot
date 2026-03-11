"""
Dashboard discovery section tests.

Verifies _build_discovery_section() and that the discovery key appears
in the _build_sniper_summary() output.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mint_ladder_bot.dashboard_server import _build_discovery_section, _build_sniper_summary, build_dashboard_payload


# ---------------------------------------------------------------------------
# _build_discovery_section with None state
# ---------------------------------------------------------------------------

def test_discovery_section_none_state_returns_zeros() -> None:
    section = _build_discovery_section(None)
    assert isinstance(section, dict)
    assert section["total_discovered"] == 0
    assert section["total_accepted"] == 0
    assert section["total_rejected"] == 0
    assert section["total_enqueued"] == 0
    assert section["source_breakdown"] == {}
    assert section["rejection_reason_breakdown"] == {}
    assert section["recent_candidates"] == []
    assert section["recent_rejected"] == []


def test_discovery_section_empty_state_returns_zeros() -> None:
    section = _build_discovery_section({})
    assert section["total_discovered"] == 0


# ---------------------------------------------------------------------------
# _build_discovery_section with populated state
# ---------------------------------------------------------------------------

def test_discovery_section_reads_stats() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    state = {
        "discovery_stats": {
            "total_discovered": 10,
            "total_accepted": 7,
            "total_rejected": 3,
            "total_enqueued": 2,
            "by_source": {"pumpfun": 8, "watchlist": 2},
            "by_rejection_reason": {"score_blocked": 3},
        },
        "discovery_recent_candidates": [
            {
                "mint": "A" * 44,
                "source_id": "pumpfun",
                "symbol": "TOKEN",
                "score": 0.75,
                "outcome": "accepted",
                "liquidity_usd": 10000.0,
                "discovered_at": now,
            }
        ],
        "discovery_rejected_candidates": [
            {
                "mint": "B" * 44,
                "source_id": "test",
                "symbol": None,
                "rejection_reason": "score_blocked",
                "score": 0.1,
                "discovered_at": now,
            }
        ],
    }

    section = _build_discovery_section(state)

    assert section["total_discovered"] == 10
    assert section["total_accepted"] == 7
    assert section["total_rejected"] == 3
    assert section["total_enqueued"] == 2
    assert section["source_breakdown"]["pumpfun"] == 8
    assert section["rejection_reason_breakdown"]["score_blocked"] == 3
    assert section["recent_accepted_count"] == 1
    assert section["recent_rejected_count"] == 1
    assert len(section["recent_candidates"]) == 1
    assert len(section["recent_rejected"]) == 1
    assert section["recent_candidates"][0]["source_id"] == "pumpfun"
    assert section["recent_rejected"][0]["rejection_reason"] == "score_blocked"


def test_discovery_section_truncates_recent_to_10() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    records = [
        {"mint": str(i) * 44, "source_id": "test", "symbol": "T", "score": 0.5, "outcome": "accepted", "liquidity_usd": None, "discovered_at": now}
        for i in range(15)
    ]
    state = {"discovery_recent_candidates": records}
    section = _build_discovery_section(state)
    assert len(section["recent_candidates"]) == 10


def test_discovery_section_truncates_rejected_to_10() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    records = [
        {"mint": str(i) * 44, "source_id": "test", "symbol": None, "rejection_reason": "score_blocked", "score": 0.0, "discovered_at": now}
        for i in range(12)
    ]
    state = {"discovery_rejected_candidates": records}
    section = _build_discovery_section(state)
    assert len(section["recent_rejected"]) == 10


# ---------------------------------------------------------------------------
# discovery key NOT in sniper summary — it is a top-level payload key
# ---------------------------------------------------------------------------

def test_sniper_summary_does_not_contain_discovery_key() -> None:
    """discovery must not be nested inside sniper_summary."""
    summary = _build_sniper_summary(None)
    assert "discovery" not in summary


def test_sniper_summary_existing_keys_unchanged() -> None:
    """All original sniper_summary keys must be present and unchanged."""
    summary = _build_sniper_summary(None)
    expected_keys = {
        "enabled", "mode", "discovery_enabled",
        "manual_seed_queue_size", "pending_attempts_count",
        "open_sniper_positions_count", "recent_success_count_1h",
        "recent_success_count_24h", "last_decision_at", "last_buy_at",
    }
    assert expected_keys.issubset(summary.keys())


def test_discovery_is_top_level_in_build_dashboard_payload(tmp_path) -> None:
    """build_dashboard_payload must expose 'discovery' as a top-level key."""
    # build_dashboard_payload reads from files; just verify the key exists when files are absent.
    payload = build_dashboard_payload(tmp_path)
    assert "discovery" in payload
    assert "sniper_summary" in payload
    # discovery must not be inside sniper_summary
    assert "discovery" not in payload["sniper_summary"]
    assert isinstance(payload["discovery"], dict)


def test_discovery_section_has_review_only_flag() -> None:
    """review_only must be present and a bool in the section."""
    section = _build_discovery_section(None)
    assert "review_only" in section
    assert isinstance(section["review_only"], bool)


def test_discovery_section_full_mint_address() -> None:
    """Mint address must be the full address, not truncated."""
    full_mint = "A" * 44
    now = datetime.now(tz=timezone.utc).isoformat()
    state = {
        "discovery_recent_candidates": [
            {"mint": full_mint, "source_id": "watchlist", "symbol": "T", "score": 0.5,
             "outcome": "accepted", "liquidity_usd": None, "discovered_at": now}
        ],
        "discovery_rejected_candidates": [
            {"mint": full_mint, "source_id": "test", "symbol": None, "rejection_reason": "score_blocked",
             "score": 0.1, "discovered_at": now}
        ],
    }
    section = _build_discovery_section(state)
    assert section["recent_candidates"][0]["mint"] == full_mint
    assert section["recent_rejected"][0]["mint"] == full_mint


def test_discovery_top_level_reads_state(tmp_path) -> None:
    """discovery top-level key reflects state when state.json is present."""
    import json
    state_data = {
        "version": 1,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "status_file": "status.json",
        "discovery_stats": {
            "total_discovered": 7,
            "total_accepted": 4,
            "total_rejected": 3,
            "total_enqueued": 1,
            "by_source": {"pumpfun": 7},
            "by_rejection_reason": {"score_blocked": 3},
        },
        "discovery_recent_candidates": [],
        "discovery_rejected_candidates": [],
    }
    (tmp_path / "state.json").write_text(json.dumps(state_data))
    payload = build_dashboard_payload(tmp_path)
    assert payload["discovery"]["total_discovered"] == 7
    assert payload["discovery"]["total_accepted"] == 4
