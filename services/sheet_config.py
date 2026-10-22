"""Read a complete key/value sheet; validate it before the trader can use it."""
# Import background execution, configuration copying, JSON, filesystem, and authentication support.
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
# Identify settings that must remain integers after spreadsheet values are parsed.
INTEGER_KEYS = frozenset({'atr.period', 'garch.window_hours', 'garch.min_observed_returns',
                          'garch.forecast_every_hours', 'garch.n_paths', 'garch.seed'})

# List the settings permitted to change while the paper account is running.
LIVE_KEYS = frozenset({
    'strategy_mode', 'dca.amount', 'dca.drop_threshold', 'swing.trade_amount', 'swing.crossover',
    'atr.multiplier', 'risk.garch_sizing_enabled', 'risk.reference_hourly_sigma',
    'risk.minimum_swing_amount', 'exits.garch_trailing_enabled',
    'exits.garch_trailing_multiplier', 'exits.bearish_crossover_enabled',
})


# Flatten nested configuration dictionaries into dotted key paths.
def flatten(config, prefix=''):

    result = {}
    # Visit each setting and build its complete path relative to the current prefix.
    for key, value in config.items():
        path = f'{prefix}.{key}' if prefix else key
        # Recurse into nested dictionaries and merge their flattened settings.
        if isinstance(value, dict):
            result.update(flatten(value, path))
        # Store leaf values directly under their dotted paths.
        else:
            result[path] = value

    return result


# Create a stable SHA-256 identifier from deterministic configuration JSON.
def config_hash(config):

    payload = json.dumps(config, sort_keys=True, allow_nan=False, separators=(',', ':'))

    return hashlib.sha256(payload.encode()).hexdigest()


# Require the template schema and types, then apply trading configuration validation.
def checked_config(candidate, template):
    """Require the complete known schema, strict types, then business validation."""
    # Reject a candidate that is not a dictionary.
    if not isinstance(candidate, dict):
        raise ValueError('Configuration must be an object.')
    
    actual, expected = flatten(candidate), flatten(template)

    # Reject missing or extra settings compared with the template.
    if actual.keys() != expected.keys():
        raise ValueError('Configuration keys differ from config.json; no settings applied.')
    
    # Validate each candidate value against its template value and any integer-only requirement.
    for key, old in expected.items():
        value = actual[key]

        # Require an actual Boolean for template Boolean settings.
        if type(old) is bool:
            valid = type(value) is bool

        # Require an actual integer for each integer-only setting.
        elif key in INTEGER_KEYS:
            valid = type(value) is int

        # Allow finite integer or float values for other numeric settings.
        elif type(old) in (int, float):
            valid = type(value) in (int, float) and math.isfinite(value)

        # Require the same Python type as the template for all remaining settings.
        else:
            valid = type(value) is type(old)

        # Reject any setting that fails its type requirement.
        if not valid:
            raise ValueError(f'Invalid type for {key}.')
        
    # Validate business constraints and return an independent copy of the configuration.
    validate_config(candidate)
    return deepcopy(candidate)


# Validate a candidate and reject changes to settings that are locked during live operation.
def require_live_compatible(candidate, current):

    candidate = checked_config(candidate, current)
    before, after = flatten(current), flatten(candidate)
    # Compare flattened configurations and collect changed keys outside the live-edit allowlist.
    locked = sorted(k for k in before if k not in LIVE_KEYS and before[k] != after[k])

    # Reject the complete update when any locked setting differs.
    if locked:
        raise ValueError('Locked settings changed: ' + ', '.join(locked))
    
    return candidate


# Convert a complete two-column key/value sheet into a validated nested configuration.
def parse_rows(rows, template):
    """A1:B1 = key,value. Numbers are fractions, booleans true/false."""

    # Require the first row to contain the key and value headers.
    if not rows or [str(x).strip().lower() for x in rows[0]] != ['key', 'value']:
        raise ValueError('Sheet header must be key and value in A1:B1.')
    
    expected = flatten(template)
    values = {}

    # Parse each data row after the header and collect one value for each known setting.
    for row in rows[1:]:
        # Skip rows that are empty or contain only blank cells.
        if not row or all(str(cell).strip() == '' for cell in row):
            continue

        # Reject rows that do not contain exactly two cells.
        if len(row) != 2:
            raise ValueError('Every setting needs exactly a key and value.')
        
        key, raw = str(row[0]).strip(), row[1]
        # Reject unknown keys and duplicate settings before applying any change.
        if key not in expected or key in values:
            raise ValueError('Unknown or duplicate setting; no settings applied.')
        
        old = expected[key]
        # Trim text cells and interpret their content according to the expected setting type.
        if isinstance(raw, str):
            raw = raw.strip()
            # Keep string settings as text rather than interpreting them as JSON.
            if type(old) is str:
                value = raw

            # Recognize case-insensitive Boolean text.
            elif raw.lower() in ('true', 'false'):
                value = raw.lower() == 'true'

            # Parse other text values using JSON numeric and literal syntax.
            else:
                # Attempt JSON decoding for the remaining non-string settings.
                try:
                    value = json.loads(raw)

                # Reject malformed cell values without including the original parsing exception.
                except (ValueError, TypeError):
                    raise ValueError(f'Invalid value for {key}.') from None
                
        # Keep non-text spreadsheet values in their supplied numeric or Boolean form.
        else:
            value = raw
        # Sheets returns whole numbers as numeric values; normalize only integer fields.
        # Convert finite whole-valued floats to integers only for integer-only settings.
        if key in INTEGER_KEYS and type(value) is float and math.isfinite(value) and value.is_integer():
            value = int(value)

        values[key] = value
    # Reject incomplete sheets rather than applying a partial configuration.
    if values.keys() != expected.keys():
        raise ValueError('Sheet is incomplete; include every row from --template.')
    
    candidate = deepcopy(template)

    # Rebuild each dotted setting path inside an independent copy of the template.
    for key, value in values.items():
        parts = key.split('.')
        target = candidate

        # Descend through parent sections to locate the dictionary containing the leaf setting.
        for part in parts[:-1]:
            target = target[part]

        # Assign the parsed value at the final component of its dotted path.
        target[parts[-1]] = value

    return checked_config(candidate, template)


# Write complete JSON to a temporary file and atomically replace the configuration cache.
def write_cache(path, config):
    """Replace only complete JSON; a crash cannot leave half a configuration."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None

    # Prepare the replacement cache while guaranteeing cleanup of a leftover temporary file.
    try:
        # Create a temporary file in the destination directory for same-filesystem replacement.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', delete=False) as handle:
            temporary = Path(handle.name)
            # Serialize, flush, and synchronize the complete configuration before replacement.
            json.dump(config, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Replace the destination only after the temporary file has been fully written.
        os.replace(temporary, path)

    # Clean up any temporary file remaining after success or failure.
    finally:
        # Remove the temporary path if one was created; tolerate its prior removal by replacement.
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# Fetch the configured Google Sheet using read-only service-account credentials.
def read_sheet(template):
    """Runs in a worker. Never logs credentials or Google response bodies."""
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession

    # Read the sheet ID, credential path, and optional range from environment variables.
    sheet_id = os.environ['GOOGLE_SHEETS_ID'].strip()
    credentials_path = os.environ['GOOGLE_APPLICATION_CREDENTIALS']
    cell_range = os.environ.get('GOOGLE_SHEETS_RANGE', 'Config!A:B')
    # Authorize read-only spreadsheet access and safely encode the sheet ID and range into the URL.
    credentials = Credentials.from_service_account_file(
        credentials_path, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    url = (f'https://sheets.googleapis.com/v4/spreadsheets/{quote(sheet_id, safe="")}'
           f'/values/{quote(cell_range, safe="")}')
    
    # Use an authenticated session with bounded authentication refresh and request timeouts.
    with AuthorizedSession(credentials, refresh_timeout=10) as session:
        response = session.get(url, params={'valueRenderOption': 'UNFORMATTED_VALUE'},
                               timeout=(5, 10))
        
        # Reject unsuccessful HTTP responses before attempting to parse configuration rows.
        if response.status_code != 200:
            raise RuntimeError(f'Sheets HTTP {response.status_code}')
        
        rows = response.json().get('values', [])

    # Validate all returned spreadsheet rows against the supplied configuration template.
    return parse_rows(rows, template)


# Poll Google Sheets in a single background worker without blocking trading on network requests.
class SheetPoller:
    """Hourly background fetch; no network call runs on the trading thread."""
    # Copy the schema template and initialize the fetch interval, worker, and job state.
    def __init__(self, template, interval=3600, fetch=read_sheet):

        self.template = deepcopy(template)
        self.interval, self.fetch = interval, fetch
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.job = None
        self.next_fetch = 0.0

    # Return a completed update only when application is allowed, and schedule due fetches.
    def tick(self, can_apply):

        # Consume a completed job only when the caller allows configuration changes.
        if self.job is not None and self.job.done() and can_apply:
            job, self.job = self.job, None

            # Retrieve the worker result, including any exception raised during fetching or validation.
            try:
                return job.result()
            
            # Log only the exception class and retain the previously saved configuration on failure.
            except Exception as error:
                # SDK exceptions can contain authentication details. Log class only.
                logging.warning('Sheet refresh rejected/unavailable (%s); retaining saved settings.',
                                type(error).__name__)
                
        # Submit a new fetch only when no job remains and the polling interval has elapsed.
        if self.job is None and time.monotonic() >= self.next_fetch:
            self.job = self.executor.submit(self.fetch, self.template)
            self.next_fetch = time.monotonic() + self.interval
            
        return None

    # Cancel queued work and request worker shutdown without waiting for an active fetch.
    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
