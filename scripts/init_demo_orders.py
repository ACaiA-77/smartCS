"""Create or repair the local SQLite domestic e-commerce demo order dataset."""

from __future__ import annotations

import argparse

from mcp.order_repository import OrderRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize the SQLite domestic e-commerce business sandbox.")
    parser.add_argument("--db-path", default="./data/orders.db", help="SQLite database path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repository = OrderRepository(args.db_path)
    with repository._connect() as connection:
        tables = ("users", "orders", "order_items", "payments", "shipments", "refunds")
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    count_text = ", ".join(f"{table}={counts[table]}" for table in tables)
    print(f"Initialized domestic e-commerce business sandbox at {repository.db_path}")
    print(f"Counts: {count_text}")


if __name__ == "__main__":
    main()
