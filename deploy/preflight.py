"""Refuse accidental fresh accounts; require an explicitly exported paper DB."""
# Import JSON and database helpers for inspecting the deployment account.
import json
from pathlib import Path
import sqlite3

# Import the configuration validator used to check saved trading settings.
from services.config_validation import validate_config

# Check the deployed paper account, companion ledger identities, and runtime write access.
def check(root = Path('/app/runtime')):

    # Refuse startup when the export directory contains an incomplete-export marker.
    if (root / 'EXPORT_FAILED').exists(): 
        raise ValueError('Incomplete account export; do not start.')
    
    # Locate the expected paper-account database inside the runtime directory.
    p = root/'paper.sqlite'
    # Reject a missing database instead of allowing accidental creation of a fresh account.
    if not p.is_file(): 
        raise ValueError('Export your existing account to docker-runtime first.')
    
    # Inspect the saved checkpoint using read-only access to the account database.
    with sqlite3.connect(p.resolve().as_uri() + '?mode=ro',uri = True) as db:
        row = db.execute('SELECT mode,config_json FROM checkpoint WHERE id=1').fetchone()
        # Require an existing checkpoint explicitly marked as a paper account.
        if not row or row[0] != 'paper':
            raise ValueError('Initialized paper account required.')

        # Decode and validate the saved configuration before permitting startup.
        validate_config(json.loads(row[1]))

    # Inspect each optional report or advisor ledger using its corresponding source-identity column.
    for suffix,table,column in [('.reports.sqlite', 'identity', 'source'),
                                ('.advisor.sqlite', 'advisor_source', 'path')]:
        ledger = Path(str(p) + suffix)
        # Validate a companion ledger only when its file exists.
        if ledger.exists():
            # Read the ledger identity without allowing modifications to its contents.
            with sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri = True) as db:
                row = db.execute(f'SELECT {column} FROM {table} WHERE id=1').fetchone()

                # Reject a ledger whose stored account path differs from the expected runtime database path.
                if not row or row[0] != str(p):
                    raise ValueError('Ledger path mismatch; use export_account.py.')
    # Create and remove a small probe file to confirm write access to the runtime directory.
    probe = root/'.write-probe'
    probe.write_text('ok'); probe.unlink()

    # Report readiness after account, configuration, identity, and write-access checks pass.
    print('Paper account and storage ready.')

# Run the preflight entry point only when this file is executed directly.
if __name__ == '__main__':
    # Prepend the project root to the module search path before invoking the check.
    import sys
    sys.path.insert(0,
                    str(Path(__file__).resolve().parents[1]))
    # Run the checks against the default container runtime directory.
    check()
