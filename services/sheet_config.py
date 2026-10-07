"""Read a complete key/value sheet; validate it before the trader can use it."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote

from services.config_validation import validate_config

# Live changes affect future decisions. Account funding and model settings need
# a deliberate migration/restart policy, so this first integration locks them.
INTEGER_KEYS = frozenset({'atr.period', 'garch.window_hours', 'garch.min_observed_returns',
                          'garch.forecast_every_hours', 'garch.n_paths', 'garch.seed'})

LIVE_KEYS = frozenset({
    'dca.amount', 'dca.drop_threshold', 'swing.trade_amount', 'swing.crossover',
    'atr.multiplier', 'risk.garch_sizing_enabled', 'risk.reference_hourly_sigma',
    'risk.minimum_swing_amount', 'exits.garch_trailing_enabled',
    'exits.garch_trailing_multiplier', 'exits.bearish_crossover_enabled',
})


def flatten(config, prefix=''):
    result = {}
    for key, value in config.items():
        path = f'{prefix}.{key}' if prefix else key
        if isinstance(value, dict):
            result.update(flatten(value, path))
        else:
            result[path] = value
    return result


def config_hash(config):
    payload = json.dumps(config, sort_keys=True, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def checked_config(candidate, template):
    """Require the complete known schema, strict types, then business validation."""
    if not isinstance(candidate, dict):
        raise ValueError('Configuration must be an object.')
    actual, expected = flatten(candidate), flatten(template)
    if actual.keys() != expected.keys():
        raise ValueError('Configuration keys differ from config.json; no settings applied.')
    for key, old in expected.items():
        value = actual[key]
        if type(old) is bool:
            valid = type(value) is bool
        elif key in INTEGER_KEYS:
            valid = type(value) is int
        elif type(old) in (int, float):
            valid = type(value) in (int, float) and math.isfinite(value)
        else:
            valid = type(value) is type(old)
        if not valid:
            raise ValueError(f'Invalid type for {key}.')
    validate_config(candidate)
    return deepcopy(candidate)


def require_live_compatible(candidate, current):
    candidate = checked_config(candidate, current)
    before, after = flatten(current), flatten(candidate)
    locked = sorted(k for k in before if k not in LIVE_KEYS and before[k] != after[k])
    if locked:
        raise ValueError('Locked settings changed: ' + ', '.join(locked))
    return candidate


def parse_rows(rows, template):
    """A1:B1 = key,value. Numbers are fractions, booleans true/false."""
    if not rows or [str(x).strip().lower() for x in rows[0]] != ['key', 'value']:
        raise ValueError('Sheet header must be key and value in A1:B1.')
    expected = flatten(template)
    values = {}
    for row in rows[1:]:
        if not row or all(str(cell).strip() == '' for cell in row):
            continue
        if len(row) != 2:
            raise ValueError('Every setting needs exactly a key and value.')
        key, raw = str(row[0]).strip(), row[1]
        if key not in expected or key in values:
            raise ValueError('Unknown or duplicate setting; no settings applied.')
        old = expected[key]
        if isinstance(raw, str):
            raw = raw.strip()
            if type(old) is str:
                value = raw
            elif raw.lower() in ('true', 'false'):
                value = raw.lower() == 'true'
            else:
                try:
                    value = json.loads(raw)
                except (ValueError, TypeError):
                    raise ValueError(f'Invalid value for {key}.') from None
        else:
            value = raw
        # Sheets returns whole numbers as numeric values; normalize only integer fields.
        if key in INTEGER_KEYS and type(value) is float and math.isfinite(value) and value.is_integer():
            value = int(value)
        values[key] = value
    if values.keys() != expected.keys():
        raise ValueError('Sheet is incomplete; include every row from --template.')
    candidate = deepcopy(template)
    for key, value in values.items():
        parts = key.split('.')
        target = candidate
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return checked_config(candidate, template)


def write_cache(path, config):
    """Replace only complete JSON; a crash cannot leave half a configuration."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_sheet(template):
    """Runs in a worker. Never logs credentials or Google response bodies."""
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    sheet_id = os.environ['GOOGLE_SHEETS_ID'].strip()
    credentials_path = os.environ['GOOGLE_APPLICATION_CREDENTIALS']
    cell_range = os.environ.get('GOOGLE_SHEETS_RANGE', 'Config!A:B')
    credentials = Credentials.from_service_account_file(
        credentials_path, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    url = (f'https://sheets.googleapis.com/v4/spreadsheets/{quote(sheet_id, safe="")}'
           f'/values/{quote(cell_range, safe="")}')
    with AuthorizedSession(credentials, refresh_timeout=10) as session:
        response = session.get(url, params={'valueRenderOption': 'UNFORMATTED_VALUE'},
                               timeout=(5, 10))
        if response.status_code != 200:
            raise RuntimeError(f'Sheets HTTP {response.status_code}')
        rows = response.json().get('values', [])
    return parse_rows(rows, template)


class SheetPoller:
    """Hourly background fetch; no network call runs on the trading thread."""
    def __init__(self, template, interval=3600, fetch=read_sheet):
        self.template = deepcopy(template)
        self.interval, self.fetch = interval, fetch
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.job = None
        self.next_fetch = 0.0

    def tick(self, can_apply):
        if self.job is not None and self.job.done() and can_apply:
            job, self.job = self.job, None
            try:
                return job.result()
            except Exception as error:
                # SDK exceptions can contain authentication details. Log class only.
                logging.warning('Sheet refresh rejected/unavailable (%s); retaining saved settings.',
                                type(error).__name__)
        if self.job is None and time.monotonic() >= self.next_fetch:
            self.job = self.executor.submit(self.fetch, self.template)
            self.next_fetch = time.monotonic() + self.interval
        return None

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
