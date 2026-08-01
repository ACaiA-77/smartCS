"""Create or repair the local SQLite domestic e-commerce demo order dataset."""

from __future__ import annotations

import argparse

from mcp.order_repository import OrderRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize 100 SQLite domestic e-commerce demo orders.")
    parser.add_argument("--db-path", default="./data/orders.db", help="SQLite database path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repository = OrderRepository(args.db_path)
    print(f"Initialized {repository.count_orders()} demo orders at {repository.db_path}")


if __name__ == "__main__":
    main()
