# Mint Ladder Bot — Actual Architecture & Current-State Readiness

CEO directive: document the **real current architecture**, not target-state aspirations.  
This file describes what the system is **actually built around today**, what is already working, what is partially implemented, and what is **not yet** the canonical production path.

---

## 1. Architecture diagram — actual current system

```text
                    ┌──────────────────────────────────────────────────────┐
                    │                    CONFIG / .env                     │
                    │  wallet, Helius, Jupiter, runtime flags, thresholds │
                    └──────────────────────┬───────────────────────────────┘
                                           │
                                           ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                                MAIN / RUNNER                              │
│                                                                            │
│  Canonical responsibilities already present:                               │
│  • clean start / runtime bootstrap                                         │
│  • status.json wallet snapshot generation                                  │
│  • state.json bootstrap from status                                        │
│  • cycle loop                                                              │
│  • tx-first wallet/history reconciliation                                  │
│  • external sell ingestion                                                 │
│  • pause/quarantine/non-tradable handling                                  │
│  • dashboard payload generation                                            │
│                                                                            │
│  Golden rule: OBSERVED CHAIN TRUTH OVERRIDES PLANNED EXECUTION             │
└───────────────┬───────────────────────────────┬────────────────────────────┘
                │                               │
                │                               │
                ▼                               ▼
┌───────────────────────────────┐   ┌──────────────────────────────────────┐
│     HELIUS-FIRST DATA LAYER   │   │         JUPITER EXECUTION PATH       │
│                               │   │                                      │
│  Primary responsibilities:    │   │  Current role:                       │
│  • wallet transaction history │   │  • quote / route / swap execution    │
│  • enhanced tx parsing        │   │  • execution intent only             │
│  • signature-linked analysis  │   │                                      │
│  • token movement inference   │   │  Non-negotiable rule:                │
│  • post-trade observation     │   │  execution does NOT create inventory │
└───────────────┬───────────────┘   └──────────────────┬───────────────────┘
                │                                      │
                └──────────────────┬───────────────────┘
                                   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                         TX-FIRST LOT / ACCOUNTING ENGINE                   │
│                                                                            │
│  Canonical truth layer:                                                    │
│  • reconstruct lots from observed transaction history                      │
│  • infer swaps including token→token cases                                 │
│  • ingest external sells                                                   │
│  • preserve inventory correctness                                          │
│  • keep same-mint external excess quarantined                              │
│  • derive tradable vs non-tradable state                                   │
│                                                                            │
│  This is the only inventory truth.                                         │
│  No synthetic lots. No assumed fills. No dashboard-derived accounting.     │
└───────────────┬───────────────────────────────┬────────────────────────────┘
                │                               │
                │                               │
                ▼                               ▼
┌───────────────────────────────┐   ┌──────────────────────────────────────┐
│         LADDER ENGINE         │   │       OBSERVABILITY / DASHBOARD       │
│                               │   │                                      │
│  • lot-based sell logic       │   │  • dashboard server                   │
│  • per-lot execution state    │   │  • dashboard payload wiring           │
│  • value / PnL summaries      │   │  • metrics and health summaries       │
│  • stablecoin/SOL normalization│  │  • bag-zero / tradable explanations   │
│                               │   │                                      │
│  Profit realization engine    │   │  Dashboard is view only, not truth    │
└───────────────┬───────────────┘   └──────────────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                           RUNTIME STATE / FILES                            │
│                                                                            │
│  Active runtime artifacts:                                                 │
│  • status.json  = observed wallet/status snapshot                          │
│  • state.json   = runtime trading/accounting state                         │
│  • events.jsonl = structured event journal                                 │
│  • run.log      = operator/runtime log                                     │
│                                                                            │
│  State is persistent, but chain observation remains the source of truth.   │
└────────────────────────────────────────────────────────────────────────────┘