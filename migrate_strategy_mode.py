"""Add strategy_mode to an existing paper account without resetting it."""

# Import migration arguments, timestamps, locking, SQLite, and configuration validation helpers.
import argparse
import json
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone

from filelock import FileLock

from services.sheet_config import (require_live_compatible,
    config_hash,
    write_cache,)


# Add a missing strategy mode to an existing paper account while preserving its saved trading state.
def main():
    # Read account and local configuration paths from the command line.
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="runtime/paper.sqlite")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    # Resolve the account path and load the local configuration used for compatibility checks.
    path = Path(args.db).resolve()
    local = json.loads(Path(args.config).read_text(encoding="utf-8"))

    # Reject a missing account database rather than creating a replacement account.
    if not path.is_file():
        raise ValueError("Database not found. Check --db; do not create a new account.")

    # Uses the same lock as the paper runner.
    # Hold the paper-runner lock so migration cannot overlap a cooperating trader process.
    with FileLock(str(path) + ".runner.lock", timeout=0):
        # Open the existing database for read/write access without allowing implicit file creation.
        db = sqlite3.connect(path.as_uri() + "?mode=rw",
            uri=True,
            timeout=10,)
        db.row_factory = sqlite3.Row

        # Inspect and migrate the checkpoint while guaranteeing connection cleanup.
        try:
            row = db.execute("SELECT * FROM checkpoint WHERE id=1").fetchone()

            # Require an initialized paper account.
            if row is None or row["mode"] != "paper":
                raise ValueError("Expected an initialized paper account.")

            # Read saved settings and determine whether strategy_mode is absent.
            saved = json.loads(row["config_json"])
            needs_migration = "strategy_mode" not in saved

            updated = dict(saved)

            # Add hybrid mode only when the account has no saved strategy_mode.
            if needs_migration:
                updated["strategy_mode"] = "hybrid"

            # Reject an unsupported saved strategy mode.
            if updated["strategy_mode"] not in ("hybrid", "dca_only", "pause_entries"):
                raise ValueError("Saved strategy_mode is invalid; no changes made.")

            # Validate the complete configuration and retain the account's
            # existing settings, including accepted Google Sheets changes.
            # Validate compatibility while retaining the account's accepted saved settings.
            require_live_compatible(updated, local)

            # Back up and migrate only accounts missing the mode field.
            if needs_migration:
                # Build a unique UTC-stamped backup filename before modifying account settings.
                suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
                backup_path = path.with_name(path.name + ".before_mode_" + suffix + ".bak")

                # SQLite's backup API includes committed WAL contents.
                backup = sqlite3.connect(backup_path)
                # Copy committed database contents using SQLite backup.
                try:
                    db.backup(backup)
                # Close the backup connection even if copying fails.
                finally:
                    backup.close()

                print("Backup:", backup_path)

                # Prepare deterministic configuration JSON, the next revision, and migration audit identifiers.
                encoded = json.dumps(updated, sort_keys=True, allow_nan=False)
                revision = row["revision"] + 1
                event_key = "migration:strategy_mode:v1"
                applied_at = time.time()

                # Save the configuration and audit records atomically.
                # Apply checkpoint and audit changes in a transaction that rolls back on any failure.
                with db:
                    # Acquire the SQLite write transaction before updating the checkpoint.
                    db.execute("BEGIN IMMEDIATE")

                    # Update only the checkpoint matching the originally read revision and configuration.
                    cursor = db.execute("""
                        UPDATE checkpoint
                        SET config_json=?, revision=?, updated_at=?
                        WHERE id=1 AND revision=? AND config_json=?
                        """,
                        (encoded,
                            revision,
                            applied_at,
                            row["revision"],
                            row["config_json"],),)

                    # Abort if another change invalidated the checkpoint comparison.
                    if cursor.rowcount != 1:
                        raise RuntimeError("Account changed during migration; rolled back.")

                    # Create configuration history storage if this account predates that table.
                    db.execute("""
                        CREATE TABLE IF NOT EXISTS config_versions (
                            id INTEGER PRIMARY KEY,
                            applied_at REAL NOT NULL,
                            checkpoint_revision INTEGER NOT NULL,
                            config_hash TEXT NOT NULL,
                            source TEXT NOT NULL,
                            config_json TEXT NOT NULL
                        )
                        """)

                    # Record the migrated configuration and its new checkpoint revision.
                    db.execute("""
                        INSERT INTO config_versions (
                            applied_at, checkpoint_revision,
                            config_hash, source, config_json
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (applied_at,
                            revision,
                            config_hash(updated),
                            "migration:strategy_mode",
                            encoded,),)

                    # Register the migration event key and timestamp for audit and duplicate protection.
                    db.execute("INSERT INTO processed_events VALUES (?, ?)",
                        (event_key, applied_at),)

                    # Record the mode addition and old/new configuration hashes in the event history.
                    db.execute("""
                        INSERT INTO events (
                            event_key, kind, payload_json
                        )
                        VALUES (?, ?, ?)
                        """,
                        (event_key,
                            "configuration",
                            json.dumps({
                                    "kind": "strategy_mode_migration",
                                    "added_mode": "hybrid",
                                    "previous_config_hash": config_hash(saved),
                                    "config_hash": config_hash(updated),
                                }),),)

                print("Migration committed. " "Balances, holdings, and orders preserved.")

            # Preserve an existing mode without repeating the migration.
            else:
                print("Mode already exists; preserved:",
                    updated["strategy_mode"],)

            # Refresh the local fallback from the accepted saved settings.
            # Rerunning repairs this export if a previous export failed.
            # Refresh the JSON fallback cache from the accepted saved configuration.
            write_cache(Path(str(path) + ".config.json"),
                updated,)
            print("Configuration cache refreshed.")

        # Close the account connection regardless of migration or cache-export outcome.
        finally:
            db.close()


# Invoke the migration entry point only when executed directly.
if __name__ == "__main__":
    main()
