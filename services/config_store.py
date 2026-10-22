"""Small extension of the existing SQLiteStore; no replacement of storage.py."""
# Import copying, serialization, audit identifiers, and configuration storage helpers.
from copy import deepcopy
from dataclasses import asdict
import json
import logging
from pathlib import Path
import time
import uuid

from services.storage import SQLiteStore, dumps
from services.sheet_config import checked_config, require_live_compatible, config_hash, write_cache


# Extend account storage with configuration history and atomic live settings updates.
class ConfigStore(SQLiteStore):
    # Initialize account storage and add the configuration audit table without replacing existing tables.
    def __init__(self, path):
        super().__init__(path)
        # An additive migration: the existing tables and portfolio remain intact.
        self.db.execute('''CREATE TABLE IF NOT EXISTS config_versions (
            id INTEGER PRIMARY KEY, applied_at REAL NOT NULL,
            checkpoint_revision INTEGER NOT NULL, config_hash TEXT NOT NULL,
            source TEXT NOT NULL, config_json TEXT NOT NULL)''')
        
        self.db.commit()

    # Prefer saved database settings, then a compatible cache, then validated local settings.
    def startup_config(self, local_config, cache_path):
        row = self.db.execute('SELECT mode,config_json FROM checkpoint WHERE id=1').fetchone()

        # Restore the authoritative configuration when an account checkpoint already exists.
        if row:
            # Restrict spreadsheet-managed configuration to a paper account.
            if row['mode'] != 'paper':
                raise ValueError('Sheets configuration is only supported for the paper account.')
            # Validate saved settings against the local schema and log a shortened configuration hash.
            saved = require_live_compatible(json.loads(row['config_json']), local_config)
            logging.info('Restoring configuration from SQLite: %s', config_hash(saved)[:12])

            return saved
        
        # Try the JSON cache only when no saved account checkpoint is available.
        if Path(cache_path).exists():
            # Read and validate cached settings before allowing them to initialize the account.
            try:
                saved = json.loads(Path(cache_path).read_text(encoding='utf-8'))
                return require_live_compatible(saved, local_config)
            
            # Ignore an invalid or unreadable cache and fall back to validated local settings.
            except (ValueError, TypeError, KeyError, OSError):
                logging.warning('Invalid/unreadable config cache; using validated config.json.')

        # Validate and copy the local configuration when no usable saved configuration exists.
        return checked_config(local_config, local_config)

    # Record the startup configuration only when configuration history is empty.
    def record_initial_config(self, config, revision):
        # Commit the initial configuration audit record as one transaction.
        with self.db:

            # Avoid inserting a second startup record for an existing configuration history.
            if not self.db.execute('SELECT 1 FROM config_versions LIMIT 1').fetchone():

                self.db.execute('''INSERT INTO config_versions
                    (applied_at,checkpoint_revision,config_hash,source,config_json)
                    VALUES(?,?,?,?,?)''', (time.time(), revision, config_hash(config),
                                           'startup', dumps(config)))

    # Apply compatible settings and cancel pending entry orders in one atomic checkpoint update.
    def apply_config(self, candidate, current, state, revision):
        """Commit settings and canceled entry intents together, or neither.

        Existing holdings, cost basis, pending sells, and protective stops are
        retained. Return a new state only after the transaction succeeds.
        """
        # Validate the candidate schema, types, business rules, and locked settings.
        candidate = require_live_compatible(candidate, current)

        # Return the existing objects and revision when the configuration has not changed.
        if candidate == current:
            return current, state, revision
        
        # Copy the engine state and clear pending buys while retaining positions, stops, and sells.
        next_state = deepcopy(state)
        next_state.pending_dca = next_state.pending_swing_buy = None
        # Create a unique audit event with the old and new configuration hashes.
        event_key = 'config:' + str(uuid.uuid4())
        stamp = time.time()
        payload = {'kind': 'config_changed', 'config_hash': config_hash(candidate),
                   'previous_hash': config_hash(current),
                   'pending_entry_policy': 'cancel; wait for a new candle signal'}
        
        # Commit the checkpoint, processed-event marker, event payload, and configuration history together.
        with self.db:
            cursor = self.db.execute('''UPDATE checkpoint
                SET config_json=?,engine_json=?,revision=revision+1,updated_at=?
                WHERE id=1 AND mode='paper' AND revision=? AND config_json=?''',
                (dumps(candidate), dumps(asdict(next_state)), stamp, revision, dumps(current)))
            
            # Reject the update if the account mode, revision, or current configuration no longer matches.
            if cursor.rowcount != 1:
                raise RuntimeError('Configuration update rejected: stale checkpoint.')
            
            # Record the configuration event and the newly committed revision in the audit history.
            self.db.execute('INSERT INTO processed_events VALUES(?,?)', (event_key, stamp))
            
            self.db.execute('INSERT INTO events(event_key,kind,payload_json) VALUES(?,?,?)',
                            (event_key, 'configuration', dumps(payload)))
            
            self.db.execute('''INSERT INTO config_versions
                (applied_at,checkpoint_revision,config_hash,source,config_json)
                VALUES(?,?,?,?,?)''', (stamp, revision + 1, config_hash(candidate),
                                       'google_sheets', dumps(candidate)))
            
        # Return the new state only after the transaction has committed successfully.
        return candidate, next_state, revision + 1


# Refresh the JSON cache without undoing an already committed database configuration.
def refresh_cache(path, config):
    # SQLite is authoritative: failure to export JSON must not undo its commit.
    # Attempt an atomic cache export of the committed settings.
    try:
        write_cache(path, config)
        
    # Log a cache write failure while leaving the saved SQLite configuration authoritative.
    except OSError:
        logging.warning('Could not write JSON config cache; SQLite settings remain saved.')
