import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import backup


class HandleCompletedBackupTests(unittest.TestCase):
    def test_runs_post_backup_command_after_successful_confluence_download(self):
        atlass = Mock()
        atlass.generate_filename.return_value = 'confluence_backup.zip'
        atlass.is_already_downloaded.return_value = None
        config = {
            'DOWNLOAD_LOCALLY': True,
            'UNZIP_BACKUP': True,
            'POST_BACKUP_COMMAND': 'restic backup /app/backups',
        }

        with patch('backup.run_post_backup_command') as run_post_backup_command:
            backup.handle_completed_backup(atlass, config, 'https://example.invalid/fileId=abc', 'confluence')

        atlass.download_file.assert_called_once_with('https://example.invalid/fileId=abc', 'confluence_backup.zip')
        atlass.unzip_backup.assert_called_once_with('confluence_backup.zip', 'confluence')
        run_post_backup_command.assert_called_once_with(config)

    def test_skips_post_backup_command_when_backup_already_exists(self):
        atlass = Mock()
        atlass.generate_filename.return_value = 'jira_backup.zip'
        atlass.is_already_downloaded.return_value = 'jira_backup.zip'
        config = {
            'DOWNLOAD_LOCALLY': True,
            'POST_BACKUP_COMMAND': 'restic backup /app/backups',
        }

        with patch('backup.run_post_backup_command') as run_post_backup_command:
            backup.handle_completed_backup(atlass, config, 'https://example.invalid/fileId=abc', 'jira')

        atlass.download_file.assert_not_called()
        run_post_backup_command.assert_not_called()


class RunPostBackupCommandTests(unittest.TestCase):
    def test_logs_stdout_and_stderr_for_successful_command(self):
        stdout = io.StringIO()
        completed_process = Mock(returncode=0, stdout='done\n', stderr='warning\n')

        with patch('backup.subprocess.run', return_value=completed_process) as subprocess_run:
            with redirect_stdout(stdout):
                backup.run_post_backup_command({'POST_BACKUP_COMMAND': 'echo done'})

        subprocess_run.assert_called_once_with('echo done', shell=True, capture_output=True, text=True)
        output = stdout.getvalue()
        self.assertIn('-> Running POST_BACKUP_COMMAND: echo done', output)
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
