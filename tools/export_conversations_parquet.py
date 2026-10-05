#!/usr/bin/env python3
"""Export PostgreSQL conversation snapshots to an analysis-friendly Parquet file."""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import psycopg


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Destination .parquet file")
    parser.add_argument(
        "--schema",
        default=os.getenv("MULTIAGENT_PG_SCHEMA", "multiagente"),
        help="PostgreSQL schema (default: MULTIAGENT_PG_SCHEMA or multiagente)",
    )
    parser.add_argument("--table", default="conversations")
    parser.add_argument("--batch-size", type=int, default=100)
    return parser.parse_args()


def checked_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def connection_kwargs() -> dict[str, object]:
    password = os.getenv("MULTIAGENT_PG_PASSWORD")
    if not password:
        raise RuntimeError("MULTIAGENT_PG_PASSWORD is not set")

    return {
        "host": os.getenv("MULTIAGENT_PG_HOST", "127.0.0.1"),
        "port": int(os.getenv("MULTIAGENT_PG_PORT", "5432")),
        "dbname": os.getenv("MULTIAGENT_PG_DB", "web"),
        "user": os.getenv("MULTIAGENT_PG_USER", "admin"),
        "password": password,
        "connect_timeout": 20,
    }


def parquet_schema(source_database: str, source_table: str) -> pa.Schema:
    exported_at = datetime.now(timezone.utc).isoformat()
    metadata = {
        b"source_database": source_database.encode(),
        b"source_table": source_table.encode(),
        b"exported_at_utc": exported_at.encode(),
        b"json_columns": b"metadata,context_jsonb",
        b"json_encoding": b"UTF-8 PostgreSQL JSONB text",
    }
    return pa.schema(
        [
            pa.field("session_id", pa.string(), nullable=False),
            pa.field("title", pa.string(), nullable=False),
            pa.field("preview", pa.string(), nullable=False),
            pa.field("username", pa.string()),
            pa.field("metadata", pa.string(), nullable=False),
            pa.field("created_at", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("updated_at", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("context_jsonb", pa.string(), nullable=False),
            pa.field("conversation_type", pa.string()),
            pa.field("archived_at", pa.timestamp("us", tz="UTC")),
        ],
        metadata=metadata,
    )


def main() -> None:
    args = parse_args()
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be greater than zero")

    schema_name = checked_identifier(args.schema, "schema")
    table_name = checked_identifier(args.table, "table")
    source_table = f"{schema_name}.{table_name}"
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    columns = (
        "session_id, title, preview, username, metadata::text AS metadata, "
        "created_at, updated_at, context_jsonb::text AS context_jsonb, "
        "conversation_type, archived_at"
    )
    query = (
        f"SELECT {columns} FROM {source_table} "
        "ORDER BY created_at, session_id"
    )

    conn_kwargs = connection_kwargs()
    arrow_schema = parquet_schema(str(conn_kwargs["dbname"]), source_table)
    writer: pq.ParquetWriter | None = None
    rows_written = 0

    try:
        with psycopg.connect(**conn_kwargs) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            with conn.cursor(name="conversation_parquet_export") as cursor:
                cursor.execute(query)
                while rows := cursor.fetchmany(args.batch_size):
                    batch = pa.RecordBatch.from_pylist(
                        [dict(zip(arrow_schema.names, row, strict=True)) for row in rows],
                        schema=arrow_schema,
                    )
                    if writer is None:
                        writer = pq.ParquetWriter(
                            output,
                            arrow_schema,
                            compression="zstd",
                            compression_level=9,
                            use_dictionary=True,
                            write_statistics=True,
                        )
                    writer.write_batch(batch)
                    rows_written += len(rows)

        if writer is None:
            empty_table = pa.Table.from_batches([], schema=arrow_schema)
            pq.write_table(empty_table, output, compression="zstd")
    except Exception:
        if writer is not None:
            writer.close()
        output.unlink(missing_ok=True)
        raise
    else:
        if writer is not None:
            writer.close()

    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"Exported {rows_written} conversations to {output} ({size_mb:.2f} MiB)")


if __name__ == "__main__":
    main()
