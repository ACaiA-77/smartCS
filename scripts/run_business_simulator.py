"""Run the deterministic business simulator for a bounded number of ticks."""

from __future__ import annotations

import argparse
import time

from mcp.order_repository import OrderRepository
from sandbox.business_simulator import BusinessSimulator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SQLite business simulator.")
    parser.add_argument("--db-path", default="./data/orders.db")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between ticks")
    parser.add_argument("--ticks", type=int, default=1, help="Number of ticks; 0 runs until Ctrl+C")
    parser.add_argument("--max-transitions", type=int, default=100)
    parser.add_argument("--create-orders", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.interval < 0 or args.ticks < 0 or args.max_transitions < 0 or args.create_orders < 0:
        raise SystemExit("interval, ticks, max-transitions, and create-orders must be non-negative")

    simulator = BusinessSimulator(OrderRepository(args.db_path))
    tick_number = 0
    try:
        while args.ticks == 0 or tick_number < args.ticks:
            tick_number += 1
            result = simulator.tick(
                max_transitions=args.max_transitions,
                create_orders=args.create_orders,
            )
            print(
                f"tick={tick_number} created={result.created_count} "
                f"transitions={result.transition_count}"
            )
            if args.ticks == 0 or tick_number < args.ticks:
                if args.interval:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
