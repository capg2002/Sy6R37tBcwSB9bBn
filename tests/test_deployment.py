# Import the test dependencies and application components used by this module.
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from filelock import FileLock, Timeout
from deploy.export_account import export


# Test account exports, ledger path updates, and safeguards against unsafe copies.
class ExportTests(unittest.TestCase):
    # Create a temporary account and companion ledgers containing source paths and test data.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / 'paper.sqlite'
        # Use a SQLite transaction that commits on success and rolls back if the block fails.
        with sqlite3.connect(self.src) as db:
            db.executescript("CREATE TABLE checkpoint(id INTEGER,mode TEXT,portfolio_json TEXT); INSERT INTO checkpoint VALUES(1,'paper','{\"cash\":10000}');")
        # Process both companion ledgers, using each ledger's table and source-path column.
        for suffix, table, col in [
            ('.reports.sqlite', 'identity', 'source'),
            ('.advisor.sqlite', 'advisor_source', 'path'),
        ]:
            # Use a SQLite transaction that commits on success and rolls back if the block fails.
            with sqlite3.connect(str(self.src) + suffix) as db:
                db.execute(f'CREATE TABLE {table}(id INTEGER,{col} TEXT, preserved TEXT)')
                db.execute(f'INSERT INTO {table} VALUES(1,?,?)', (str(self.src), 'keep-me'))

    # Close test resources and remove the temporary files created for this test.
    def tearDown(self):
        self.tmp.cleanup()

    # Check that export updates copied ledger paths while preserving account data and source files.
    def test_copy_preserves_account_and_source(self):
        # Choose an export directory and retain the original account bytes for comparison.
        dest = self.root / 'export'
        before = self.src.read_bytes()
        export(self.src, dest, '/app/runtime/paper.sqlite')
        # Verify the expected results and safeguards for this scenario.
        self.assertEqual(before, self.src.read_bytes())
        # Process both companion ledgers, using each ledger's table and source-path column.
        for suffix, table, col in [
            ('.reports.sqlite', 'identity', 'source'),
            ('.advisor.sqlite', 'advisor_source', 'path'),
        ]:
            # Use a SQLite transaction that commits on success and rolls back if the block fails.
            with sqlite3.connect(dest / ('paper.sqlite' + suffix)) as db:
                # Verify the expected results and safeguards for this scenario.
                self.assertEqual(db.execute(f'SELECT {col},preserved FROM {table}').fetchone(),
                    ('/app/runtime/paper.sqlite', 'keep-me'),)
            # Use a SQLite transaction that commits on success and rolls back if the block fails.
            with sqlite3.connect(str(self.src) + suffix) as db:
                # Verify the expected results and safeguards for this scenario.
                self.assertEqual(db.execute(f'SELECT {col} FROM {table}').fetchone()[0], str(self.src))
        # Use a SQLite transaction that commits on success and rolls back if the block fails.
        with sqlite3.connect(dest / 'paper.sqlite') as db:
            # Verify the expected results and safeguards for this scenario.
            self.assertEqual(json.loads(db.execute('SELECT portfolio_json FROM checkpoint').fetchone()[0]),
                {'cash': 10000},)
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            export(self.src, dest, '/app/runtime/paper.sqlite')

    # Check that an already exported Docker account can be exported to another destination.
    def test_export_docker_account_again(self):
        # Use separate destinations for the initial export and a subsequent export of that copy.
        first = self.root / 'first'
        second = self.root / 'second'
        export(self.src, first, '/app/runtime/paper.sqlite')
        export(first / 'paper.sqlite',
            second,
            '/app/runtime/paper.sqlite',
            '/app/runtime/paper.sqlite',)
        # Verify the expected results and safeguards for this scenario.
        self.assertTrue((second / 'migration.json').exists())

    # Check that an active runner lock prevents export before a destination is created.
    def test_active_trader_rejected(self):
        # Hold the runner lock to simulate an account currently in use by a trader.
        with FileLock(str(self.src) + '.runner.lock'):
            # Require Timeout from this rejected operation; a normal return fails the test.
            with self.assertRaises(Timeout):
                export(self.src, self.root / 'export', '/app/runtime/paper.sqlite')
        # Verify the expected results and safeguards for this scenario.
        self.assertFalse((self.root / 'export').exists())

    # Check that an unrelated report ledger is rejected before any export directory is created.
    def test_wrong_ledger_rejected_before_copy(self):
        # Use a SQLite transaction that commits on success and rolls back if the block fails.
        with sqlite3.connect(str(self.src) + '.reports.sqlite') as db:
            db.execute("UPDATE identity SET source='wrong'")
        # Require ValueError from this rejected operation; a normal return fails the test.
        with self.assertRaises(ValueError):
            export(self.src, self.root / 'export', '/app/runtime/paper.sqlite')
        # Verify the expected results and safeguards for this scenario.
        self.assertFalse((self.root / 'export').exists())


# Run this file's unittest suite only when the file is executed directly.
if __name__ == '__main__':
    unittest.main()
