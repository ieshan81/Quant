#!/usr/bin/env python3
"""CLI for broker account transition preview/apply."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Broker transition / runtime sync wizard")
    parser.add_argument("mode", choices=["preview", "apply", "status", "history"])
    parser.add_argument("--confirmation", default="", help="Typed confirmation phrase")
    parser.add_argument("--transition-type", default="", help="Acknowledged transition type")
    parser.add_argument("--production-url", default="", help="Production URL for post-apply audit")
    parser.add_argument("--no-audit", action="store_true")
    parser.add_argument("--ack-open-orders", action="store_true")
    parser.add_argument("--ack-broker-positions", action="store_true")
    args = parser.parse_args()

    from monitoring.broker_transition_service import (
        apply_broker_transition,
        build_transition_status,
        fetch_transition_history,
        preview_broker_transition,
    )

    if args.mode == "preview":
        out = preview_broker_transition()
    elif args.mode == "status":
        out = build_transition_status()
    elif args.mode == "history":
        out = {"history": fetch_transition_history()}
    else:
        out = apply_broker_transition(
            transition_type_acknowledged=args.transition_type,
            confirmation_text=args.confirmation,
            backup_first=True,
            run_acceptance_audit=not args.no_audit,
            production_audit_url=args.production_url or None,
            acknowledged_open_orders=args.ack_open_orders,
            acknowledged_broker_positions=args.ack_broker_positions,
        )

    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
