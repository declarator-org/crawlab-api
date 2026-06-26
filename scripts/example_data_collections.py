#!/usr/bin/env python3
"""Example: working with data collections via the Crawlab API."""

import os
from pathlib import Path

from crawlab_api import CrawlabClient, Op

# Load from .env at repo root
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

BASE_URL = os.getenv("CRAWLAB_BASE_URL", "http://localhost:8080/api")
TOKEN = os.getenv("CRAWLAB_TOKEN", "")

if not TOKEN:
    print("Error: CRAWLAB_TOKEN not set in .env")
    exit(1)

def main():
    client = CrawlabClient(BASE_URL, TOKEN)

    # List all data collections
    print("=== All data collections ===")
    collections = client.list_data_collections(fetch_all=True)
    print(f"Total: {len(collections)}")
    for col in collections:
        print(f"  - {col['name']}: {col['id']}")

    # Example 1: Create a collection with dedup (ignore duplicates)
    print("\n=== Create collection with dedup (ignore) ===")
    col_ignore = client.create_data_collection(
        name="example_products_ignore",
        fields=[
            {"name": "product_id", "type": "string"},
            {"name": "title", "type": "string"},
            {"name": "price", "type": "float"},
        ],
        dedup={
            "enabled": True,
            "keys": ["product_id"],
            "type": "ignore",  # skip duplicate product_ids
        },
    )
    col1_id = col_ignore["id"]
    print(f"Created: {col_ignore['name']} (id={col1_id})")
    print(f"  Dedup: {col_ignore['dedup']}")

    # Example 2: Create a collection with dedup (overwrite duplicates)
    print("\n=== Create collection with dedup (overwrite) ===")
    col_overwrite = client.create_data_collection(
        name="example_products_overwrite",
        fields=[
            {"name": "product_id", "type": "string"},
            {"name": "title", "type": "string"},
            {"name": "price", "type": "float"},
        ],
        dedup={
            "enabled": True,
            "keys": ["product_id"],
            "type": "overwrite",  # update if product_id exists
        },
    )
    col2_id = col_overwrite["id"]
    print(f"Created: {col_overwrite['name']} (id={col2_id})")

    # Example 3: Create a collection without dedup
    print("\n=== Create collection without dedup ===")
    col_no_dedup = client.create_data_collection(
        name="example_raw_logs",
        # no dedup specified — all rows inserted
    )
    col3_id = col_no_dedup["id"]
    print(f"Created: {col_no_dedup['name']} (id={col3_id})")

    # Example 4: Demonstrate validation (will raise ValueError)
    print("\n=== Validation example: enabled=True but empty keys ===")
    try:
        client.create_data_collection(
            name="invalid_example",
            dedup={"enabled": True, "keys": []},  # ERROR: empty keys!
        )
    except ValueError as e:
        print(f"✓ Caught validation error: {e}")

    # Fetch and inspect a collection
    print(f"\n=== Fetch collection ===")
    fetched = client.get_data_collection(col1_id)
    print(f"Name: {fetched['name']}")
    print(f"Fields: {len(fetched.get('fields', []))} field(s)")
    dedup_config = fetched.get('dedup', {})
    print(f"Dedup enabled: {dedup_config.get('enabled')}")
    print(f"Dedup keys: {dedup_config.get('keys')}")
    print(f"Dedup type: {dedup_config.get('type')}")

    # Update a collection (change dedup type)
    print(f"\n=== Update collection ===")
    updated = client.update_data_collection(
        col1_id,
        dedup={
            "enabled": True,
            "keys": ["product_id"],
            "type": "overwrite",  # changed from "ignore" to "overwrite"
        },
    )
    print(f"Updated dedup type: {updated['dedup']['type']}")

    # Filter collections by name
    print(f"\n=== Filter by name ===")
    filtered = client.list_data_collections(
        conditions=[{"key": "name", "op": Op.CONTAINS, "value": "example"}]
    )
    print(f"Found {len(filtered)} collection(s) with 'example' in the name")
    for col in filtered:
        dedup = col.get('dedup', {})
        dedup_status = "enabled" if dedup.get('enabled') else "disabled"
        print(f"  - {col['name']} (dedup: {dedup_status})")

    # Iterate through collections
    print(f"\n=== Iterate through collections (paginated) ===")
    count = 0
    for col in client.iter_data_collections(size=5):
        count += 1
        print(f"  - {col['name']}")
    print(f"Total iterated: {count}")

    client.close()

if __name__ == "__main__":
    main()
