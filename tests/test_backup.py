import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import backup


class HandleCompletedBackupTests(unittest.TestCase):
    def test_runs_post_backup_command_after_successful_backup(self):
        atlas = Mock()
        atlas.generate_filename.return_value = 'confluence_backup.zip'
        atlas.is_already_downloaded.return_value = None
        config = {
            'DOWNLOAD_LOCALLY': True,
            'UNZIP_BACKUP': True,
            'POST_BACKUP_COMMAND': 'restic backup /app/backups',
        }

        with patch('backup.run_post_backup_command') as run_post_backup_command:
            backup.handle_completed_backup(atlas, config, 'https://example.invalid/fileId=abc', 'confluence')

        expected_backup_path = os.path.join(os.path.dirname(os.path.abspath(backup.__file__)), 'backups', 'confluence')
        atlas.download_file.assert_called_once_with('https://example.invalid/fileId=abc', 'confluence_backup.zip')
        atlas.unzip_backup.assert_called_once_with('confluence_backup.zip', 'confluence')
        run_post_backup_command.assert_called_once_with(config, backup_path=expected_backup_path, backup_type='confluence')

    def test_runs_post_backup_command_with_zip_path_when_unzip_disabled(self):
        atlas = Mock()
        atlas.generate_filename.return_value = 'jira_backup.zip'
        atlas.is_already_downloaded.return_value = None
        config = {
            'DOWNLOAD_LOCALLY': True,
            'UNZIP_BACKUP': False,
            'POST_BACKUP_COMMAND': 'echo done',
        }

        with patch('backup.run_post_backup_command') as run_post_backup_command:
            backup.handle_completed_backup(atlas, config, 'https://example.invalid/fileId=abc', 'jira')

        expected_backup_path = os.path.join(os.path.dirname(os.path.abspath(backup.__file__)), 'backups', 'jira_backup.zip')
        atlas.download_file.assert_called_once_with('https://example.invalid/fileId=abc', 'jira_backup.zip')
        atlas.unzip_backup.assert_not_called()
        run_post_backup_command.assert_called_once_with(config, backup_path=expected_backup_path, backup_type='jira')

    def test_skips_post_backup_command_when_backup_already_exists(self):
        atlas = Mock()
        atlas.generate_filename.return_value = 'jira_backup.zip'
        atlas.is_already_downloaded.return_value = 'jira_backup.zip'
        config = {
            'DOWNLOAD_LOCALLY': True,
            'POST_BACKUP_COMMAND': 'restic backup /app/backups',
        }

        with patch('backup.run_post_backup_command') as run_post_backup_command:
            backup.handle_completed_backup(atlas, config, 'https://example.invalid/fileId=abc', 'jira')

        atlas.download_file.assert_not_called()
        run_post_backup_command.assert_not_called()


class RunPostBackupCommandTests(unittest.TestCase):
    def test_logs_stdout_and_stderr_for_successful_command(self):
        stdout = io.StringIO()
        completed_process = Mock(returncode=0, stdout='done\n', stderr='warning\n')
        backup_path = '/app/backups/jira_backup.zip'
        backup_dir = '/app/backups'

        with patch('backup.subprocess.run', return_value=completed_process) as subprocess_run:
            with redirect_stdout(stdout):
                backup.run_post_backup_command(
                    {'POST_BACKUP_COMMAND': 'echo {backup_filename} {backup_type} {backup_dir} {backup_path}'},
                    backup_path=backup_path,
                    backup_type='jira'
                )

        subprocess_run.assert_called_once()
        called_command = subprocess_run.call_args.args[0]
        called_env = subprocess_run.call_args.kwargs['env']
        self.assertEqual(called_command, 'echo jira_backup.zip jira /app/backups /app/backups/jira_backup.zip')
        self.assertEqual(called_env['BACKUP_PATH'], backup_path)
        self.assertEqual(called_env['BACKUP_FILENAME'], 'jira_backup.zip')
        self.assertEqual(called_env['BACKUP_TYPE'], 'jira')
        self.assertEqual(called_env['BACKUP_DIR'], backup_dir)
        output = stdout.getvalue()
        self.assertIn('-> Running POST_BACKUP_COMMAND: echo jira_backup.zip jira /app/backups /app/backups/jira_backup.zip', output)
        self.assertIn('-> POST_BACKUP_COMMAND stdout:', output)
        self.assertIn('done', output)
        self.assertIn('-> POST_BACKUP_COMMAND stderr:', output)
        self.assertIn('warning', output)
        self.assertNotIn('exited with code', output)

    def test_warns_but_does_not_raise_for_non_zero_exit(self):
        stdout = io.StringIO()
        completed_process = Mock(returncode=23, stdout='', stderr='restic failed\n')

        with patch('backup.subprocess.run', return_value=completed_process):
            with redirect_stdout(stdout):
                backup.run_post_backup_command({'POST_BACKUP_COMMAND': 'restic backup /app/backups'})

        output = stdout.getvalue()
        self.assertIn('-> Warning: POST_BACKUP_COMMAND exited with code 23', output)
        self.assertIn('restic failed', output)

    def test_runs_command_unchanged_when_placeholder_substitution_fails(self):
        stdout = io.StringIO()
        completed_process = Mock(returncode=0, stdout='', stderr='')

        with patch('backup.subprocess.run', return_value=completed_process) as subprocess_run:
            with redirect_stdout(stdout):
                backup.run_post_backup_command({'POST_BACKUP_COMMAND': 'echo {1..3}'})

        self.assertEqual(subprocess_run.call_args.args[0], 'echo {1..3}')
        output = stdout.getvalue()
        self.assertIn('placeholder substitution failed', output)


def _make_atlas(tmp_dir):
    """Create a minimal Atlassian instance whose backups dir is tmp_dir."""
    config = {
        'USER_EMAIL': 'user@example.com',
        'API_TOKEN': 'token',
        'HOST_URL': 'example.atlassian.net',
        'INCLUDE_ATTACHMENTS': 'true',
    }
    atlas = backup.Atlassian(config)
    # Redirect the backups dir to tmp_dir
    atlas._backups_dir = tmp_dir
    return atlas


class BackupRegistryTests(unittest.TestCase):
    """Tests for the local UUID registry written by _record_uuid_in_registry."""

    UUID = '12345678-1234-1234-1234-123456789abc'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        config = {
            'USER_EMAIL': 'u@example.com',
            'API_TOKEN': 't',
            'HOST_URL': 'x.atlassian.net',
            'INCLUDE_ATTACHMENTS': 'true',
        }
        self.atlas = backup.Atlassian(config)

    def _set_registry_path(self):
        """Patch _registry_path so it uses the temp directory."""
        registry_path = os.path.join(self.tmp, '.backup_registry.json')
        self.atlas._registry_path = lambda: registry_path
        return registry_path

    def test_record_uuid_creates_registry_with_entry(self):
        self._set_registry_path()
        self.atlas._record_uuid_in_registry(self.UUID, filename='jira_file.zip', backup_type='jira')
        registry = self.atlas._load_registry()
        self.assertIn(self.UUID, registry)
        entry = registry[self.UUID]
        self.assertEqual(entry['filename'], 'jira_file.zip')
        self.assertEqual(entry['backup_type'], 'jira')
        self.assertIn('downloaded_at', entry)

    def test_record_uuid_appends_without_overwriting_others(self):
        self._set_registry_path()
        other_uuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.atlas._record_uuid_in_registry(other_uuid, filename='old.zip', backup_type='confluence')
        self.atlas._record_uuid_in_registry(self.UUID, filename='new.zip', backup_type='jira')
        registry = self.atlas._load_registry()
        self.assertIn(other_uuid, registry)
        self.assertIn(self.UUID, registry)

    def test_load_registry_returns_empty_dict_when_file_missing(self):
        self._set_registry_path()
        result = self.atlas._load_registry()
        self.assertEqual(result, {})

    def test_load_registry_returns_empty_dict_on_corrupt_json(self):
        registry_path = self._set_registry_path()
        os.makedirs(os.path.dirname(registry_path), exist_ok=True)
        with open(registry_path, 'w') as f:
            f.write('NOT JSON{{')
        result = self.atlas._load_registry()
        self.assertEqual(result, {})


class IsAlreadyDownloadedRegistryTests(unittest.TestCase):
    """Tests that is_already_downloaded checks the registry for unzipped backups."""

    UUID = '12345678-1234-1234-1234-123456789abc'
    BACKUP_URL = 'https://x.atlassian.net/plugins/servlet/export/download?fileId=12345678-1234-1234-1234-123456789abc'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        config = {
            'USER_EMAIL': 'u@example.com',
            'API_TOKEN': 't',
            'HOST_URL': 'x.atlassian.net',
            'INCLUDE_ATTACHMENTS': 'true',
        }
        self.atlas = backup.Atlassian(config)
        registry_path = os.path.join(self.tmp, '.backup_registry.json')
        self.atlas._registry_path = lambda: registry_path

    def _write_registry(self, data):
        path = self.atlas._registry_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f)

    def test_returns_filename_from_registry_when_zip_absent(self):
        self._write_registry({
            self.UUID: {'filename': 'jira_01052026_1030_{}.zip'.format(self.UUID), 'backup_type': 'jira', 'downloaded_at': '2026-05-01T10:30:00'},
        })
        # Patch out filesystem check so only the registry path is exercised
        with patch('os.path.isdir', return_value=False):
            result = self.atlas.is_already_downloaded(self.BACKUP_URL)
        self.assertEqual(result, 'jira_01052026_1030_{}.zip'.format(self.UUID))

    def test_returns_none_when_uuid_not_in_registry_or_filesystem(self):
        self._write_registry({})
        with patch('os.path.isdir', return_value=False):
            result = self.atlas.is_already_downloaded(self.BACKUP_URL)
        self.assertIsNone(result)


class UnzipBackupRegistryTests(unittest.TestCase):
    """Tests that unzip_backup records the UUID in the registry."""

    UUID = '12345678-1234-1234-1234-123456789abc'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        config = {
            'USER_EMAIL': 'u@example.com',
            'API_TOKEN': 't',
            'HOST_URL': 'x.atlassian.net',
            'INCLUDE_ATTACHMENTS': 'true',
        }
        self.atlas = backup.Atlassian(config)
        registry_path = os.path.join(self.tmp, '.backup_registry.json')
        self.atlas._registry_path = lambda: registry_path

    def test_unzip_records_uuid_in_registry(self):
        import zipfile

        local_filename = 'jira_01052026_1030_{}.zip'.format(self.UUID)
        backups_dir = os.path.join(self.tmp, 'backups')
        os.makedirs(backups_dir, exist_ok=True)

        zip_path = os.path.join(backups_dir, local_filename)
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('backup.xml', '<backup/>')

        with patch('backup.os.path.dirname', return_value=self.tmp), \
             patch('backup.os.path.abspath', side_effect=lambda p: p):
            self.atlas.unzip_backup(local_filename, 'jira')

        registry = self.atlas._load_registry()
        self.assertIn(self.UUID, registry)
        self.assertEqual(registry[self.UUID]['backup_type'], 'jira')
        # zip should have been removed
        self.assertFalse(os.path.exists(zip_path))
