"""Offline copy/backup. Source stays unchanged. Do not run with any account worker active."""
# Import command-line parsing, database backup, file locking, and migration metadata utilities.
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from filelock import FileLock

# Export an existing paper account and its available ledgers into a new deployment directory.
def export(source, destination, target, expected_source = None):

    # Resolve the source and destination to absolute paths before checking or copying files.
    source = Path(source).resolve(); destination = Path(destination).resolve()

    # Use the resolved source path as the expected ledger identity unless an explicit path is supplied.
    expected_source = expected_source or str(source)

    # Require an absolute expected source path for ledger identity comparisons.
    if not str(expected_source).startswith('/'): 
        raise ValueError('Expected source must be absolute.')
    # Reject a missing source database before attempting an export.
    if not source.is_file():
        raise ValueError('Source database not found.')

    # Require a new destination directory to avoid overwriting an earlier export.
    if destination.exists():
        raise ValueError('Destination must not exist; choose a new folder.')
    
    # Require an absolute target path ending in /paper.sqlite for the copied ledger identities.
    if not target.startswith('/') or not target.endswith('/paper.sqlite'):
        raise ValueError('Target must be an absolute paper.sqlite path.')
    
    # Hold all acquired worker locks until export finishes, releasing them even after an error.
    with ExitStack() as stack:

        # Acquire runner, notifier, report, and advisor locks without waiting; an occupied lock aborts export.
        for suffix in ('.runner.lock','.notifier.lock','.reports.sqlite.lock','.advisor.sqlite.lock'):
            stack.enter_context(FileLock(str(source) + suffix,
                                         timeout=0))
        # Read the source checkpoint without allowing database writes, then close the connection.
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as db:
            row = db.execute(
                'SELECT mode FROM checkpoint WHERE id=1'
                ).fetchone()
            
            # Reject sources that lack an initialized paper-account checkpoint.
            if not row or row[0] != 'paper':
                raise ValueError('Source is not an initialized paper account.')
            
        # Collect the account database and validated companion ledgers before creating the destination.
        pairs=[]
        
        # Inspect the main account and each optional ledger using its identity table and source-path column.
        for suffix,table,column in [
            ('', None, None),
            ('.reports.sqlite', 'identity', 'source'),
            ('.advisor.sqlite', 'advisor_source', 'path')]:

            src = Path(str(source) + suffix)
            # Skip an optional companion file that is absent from the source account.
            if not src.exists():
                continue
            # Check source identity for companion ledgers; the main account has no companion identity table.
            if table:
                # Open the companion ledger read-only and close it after retrieving its source identity.
                with closing(sqlite3.connect(src.as_uri() + '?mode=ro',uri=True)) as db:
                    row = db.execute(
                        f'SELECT {column} FROM {table} WHERE id=1'
                        ).fetchone()
                    
                    # Reject a missing or mismatched ledger identity before creating any export files.
                    if not row or row[0] != expected_source:
                        raise ValueError('Source ledger identity mismatch; no export made.')
                    
            # Retain the validated source path and ledger metadata for the later backup stage.
            pairs.append(
                (src,suffix,table,column)
                )
            
        # Create the new destination and initialize the exported-database checksum records.
        destination.mkdir(parents = True)
        records=[]

        # Create the backups and metadata while marking any incomplete export if a step fails.
        try:
            # Back up each validated database under its standard deployment filename.
            for src, suffix, table, column in pairs:
                dst = destination/('paper.sqlite' + suffix)
                # Open a read-only source and writable destination, closing both connections after the backup.
                with closing(sqlite3.connect(src.as_uri() + '?mode=ro',
                                             uri = True)) as a, closing(sqlite3.connect(dst)) as b:
                    # Use SQLite backup to copy database contents rather than copying an active database file directly.
                    a.backup(b)
                    # Reject a copied database whose SQLite integrity check does not return ok.
                    if b.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise ValueError('Integrity check failed.')
                    
                    # Update only the copied companion ledger source path and commit that change.
                    if table:
                        b.execute(f'UPDATE {table} SET {column}=? WHERE id=1',(target,)); b.commit()

                # Record the final copied database filename and SHA-256 checksum after its connection closes.
                records.append({
                    'file':dst.name,'sha256':hashlib.sha256(dst.read_bytes()
                                                            ).hexdigest()})
            # Locate the optional cached configuration associated with the source account.
            cache = Path(str(source) + '.config.json')

            # Copy the configuration cache when present, preserving its filesystem metadata where supported.
            if cache.exists():
                shutil.copy2(cache,
                             destination/'paper.sqlite.config.json')
            # Write migration metadata containing UTC creation time, account paths, and database checksums.
            (destination/'migration.json').write_text(
                json.dumps({'created_at':datetime.now(timezone.utc
                                                      ).isoformat(),
                'source': str(source),
                'target':target,
                'files':records,
                'note':'Only copied ledger source paths changed; delivery IDs and statuses preserved.'},
                indent=2))
        # Mark the destination as an incomplete export and re-raise the original export failure.
        except Exception:
            # Incomplete exports are retained for inspection, never treated as ready.
            (destination/'EXPORT_FAILED').write_text('Do not deploy this incomplete export.')
            raise
    # Print the ready message only after all export steps finish successfully.
    print('Export ready:',destination)

# Parse command-line options and run the export only when this file is executed directly.
if __name__ == '__main__':
    # Define source, destination, target, and optional stored-source identity arguments.
    p = argparse.ArgumentParser(description = __doc__)
    p.add_argument('--source', default = 'runtime/paper.sqlite')
    p.add_argument('--destination', required = True)
    p.add_argument('--target', default = '/app/runtime/paper.sqlite')
    p.add_argument('--expected-source', help = 'Exact stored ledger source path; only for relocating a verified account copy.')
    # Parse the supplied arguments and pass them to the export function.
    a = p.parse_args(); export(a.source,
                               a.destination,
                               a.target,
                               a.expected_source)
