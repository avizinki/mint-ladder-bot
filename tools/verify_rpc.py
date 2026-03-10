#!/usr/bin/env python3
"""Verify RPC: getHealth, getSignaturesForAddress, getTransaction. Uses .env via main."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

# Load .env
for p in (root / ".env", Path.cwd() / ".env"):
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in __import__("os").environ:
                    __import__("os").environ[k] = v
        break

from mint_ladder_bot.config import Config
from mint_ladder_bot.rpc import RpcClient

def main():
    c = Config()
    rpc = RpcClient(c.rpc_endpoint, timeout_s=25.0, max_retries=5)
    ok, lat = rpc.validate()
    print("getHealth:", "OK" if ok else "FAIL", "latency_ms=", lat)
    if not ok:
        rpc.close()
        return 1
    status_path = root / "status.json"
    if not status_path.exists():
        print("status.json not found")
        rpc.close()
        return 1
    status = json.loads(status_path.read_text())
    wallet = status.get("wallet")
    if not wallet:
        print("No wallet in status.json")
        rpc.close()
        return 1
    try:
        sigs = rpc.get_signatures_for_address(wallet, limit=15)
        print("getSignaturesForAddress: OK count=", len(sigs))
    except Exception as e:
        print("getSignaturesForAddress FAIL:", type(e).__name__, str(e)[:150])
        rpc.close()
        return 1
    if not sigs:
        print("getTransaction: skip (no sigs)")
        rpc.close()
        return 0
    sig = sigs[0].get("signature")
    if not sig:
        print("getTransaction: skip (no signature in first item)")
        rpc.close()
        return 0
    try:
        tx = rpc.get_transaction(sig)
        print("getTransaction: OK" if tx else "getTransaction: FAIL (null)")
    except Exception as e:
        print("getTransaction FAIL:", type(e).__name__, str(e)[:150])
        rpc.close()
        return 1
    rpc.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
