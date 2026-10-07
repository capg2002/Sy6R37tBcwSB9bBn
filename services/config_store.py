"""Small extension of the existing SQLiteStore; no replacement of storage.py."""
from copy import deepcopy
from dataclasses import asdict
import json
import logging
from pathlib import Path
import time
import uuid

from services.storage import SQLiteStore, dumps
from services.sheet_config import checked_config, require_live_compatible, config_hash, write_cache


class ConfigStore(SQLiteStore):
    def __init__(self, path):
        super().__init__(path)
        # An additive migration: the existing tables and portfolio remain intact.
        self.db.execute('''CREATE TABLE IF NOT EXISTS config_versions (
            id INTEGER PRIMARY KEY, applied_at REAL NOT NULL,
            checkpoint_revision INTEGER NOT NULL, config_hash TEXT NOT NULL,
            source TEXT NOT NULL, config_json TEXT NOT NULL)''')
        self.db.commit()

    def startup_config(self, local_config, cache_path):
        row = self.db.execute('SELECT mode,config_json FROM checkpoint WHERE id=1').fetchone()
        if row:
            if row['mode'] != 'paper':
                raise ValueError('Sheets configuration is only supported for the paper account.')
            saved = require_live_compatible(json.loads(row['config_json']), local_config)
            logging.info('Restoring configuration from SQLite: %s', config_hash(saved)[:12])
            return saved
        if Path(cache_path).exists():
            try:
                saved = json.loads(Path(cache_path).read_text(encoding='utf-8'))
                return require_live_compatible(saved, local_config)
            except (ValueError, TypeError, KeyError, OSError):
                logging.warning('Invalid/unreadable config cache; using validated config.json.')
        return checked_config(local_config, local_config)

    def record_initial_config(self, config, revision):
        with self.db:
            if not self.db.execute('SELECT 1 FROM config_versions LIMIT 1').fetchone():
                self.db.execute('''INSERT INTO config_versions
                    (applied_at,checkpoint_revision,config_hash,source,config_json)
                    VALUES(?,?,?,?,?)''', (time.time(), revision, config_hash(config),
                                           'startup', dumps(config)))

    def apply_config(self, candidate, current, state, revision):
        """Commit settings and canceled entry intents together, or neither.

        Existing holdings, cost basis, pending sells, and protective stops are
        retained. Return a new state only after the transaction succeeds.
        """
        candidate = require_live_compatible(candidate, current)
        if candidate == current:
            return current, state, revision
        next_state = deepcopy(state)
        next_state.pending_dca = next_state.pending_swing_buy = None
        event_key = 'config:' + str(uuid.uuid4())
        stamp = time.time()
        payload = {'kind': 'config_changed', 'config_hash': config_hash(candidate),
                   'previous_hash': config_hash(current),
                   'pending_entry_policy': 'cancel; wait for a new candle signal'}
        with self.db:
            cursor = self.db.execute('''UPDATE checkpoint
                SET config_json=?,engine_json=?,revision=revision+1,updated_at=?
                WHERE id=1 AND mode='paper' AND revision=? AND config_json=?''',
                (dumps(candidate), dumps(asdict(next_state)), stamp, revision, dumps(current)))
            if cursor.rowcount != 1:
                raise RuntimeError('Configuration update rejected: stale checkpoint.')
            self.db.execute('INSERT INTO processed_events VALUES(?,?)', (event_key, stamp))
            self.db.execute('INSERT INTO events(event_key,kind,payload_json) VALUES(?,?,?)',
                            (event_key, 'configuration', dumps(payload)))
            self.db.execute('''INSERT INTO config_versions
                (applied_at,checkpoint_revision,config_hash,source,config_json)
                VALUES(?,?,?,?,?)''', (stamp, revision + 1, config_hash(candidate),
                                       'google_sheets', dumps(candidate)))
        return candidate, next_state, revision + 1


def refresh_cache(path, config):
    # SQLite is authoritative: failure to export JSON must not undo its commit.
    try:
        write_cache(path, config)
    except OSError:
        logging.warning('Could not write JSON config cache; SQLite settings remain saved.')
