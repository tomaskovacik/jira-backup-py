import os
import yaml


def _data_dir():
    """Directory for user data: config.yaml, playwright_cookies.json, backups/.

    Overridable via the DATA_DIR env var for containerized deployments where
    the mounted data volume differs from the script's own location; defaults
    to the script's directory otherwise (unchanged local, non-Docker usage).
    """
    return os.environ.get('DATA_DIR') or os.path.dirname(os.path.abspath(__file__))


def _load_existing_config(config_path):
    """Load the existing config.yaml, if any, to use as defaults for the wizard."""
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, IOError):
        return {}


def _ask(prompt, current='') -> str:
    """Prompt the user, showing the current value (if any) as the default on Enter."""
    current = '' if current is None else str(current)
    suffix = f' [{current}]' if current != '' else ''
    answer = input(f'{prompt}{suffix}: ').strip()
    return answer if answer else current


def create_config():
    config_path = os.path.join(_data_dir(), 'config.yaml')
    existing = _load_existing_config(config_path)
    existing_s3 = existing.get('UPLOAD_TO_S3') or {}

    if existing:
        print('-> Existing config.yaml found. Press Enter to keep the current value shown in [brackets].\n')

    jira_host = _ask('What is your Jira host name?', existing.get('HOST_URL', ''))
    user = _ask('What is your Jira account email address?', existing.get('USER_EMAIL', ''))
    password = _ask('Paste your Jira API token', existing.get('API_TOKEN', ''))
    attachments = _ask('Do you want to include attachments? (true / false)', existing.get('INCLUDE_ATTACHMENTS', ''))
    download_locally = _ask('Do you want to download the backup file locally? (true / false)', existing.get('DOWNLOAD_LOCALLY', ''))

    use_playwright = _ask(
        'Do you want to use Playwright web UI mode instead of REST API? (true / false)',
        str(existing.get('USE_PLAYWRIGHT', '')).lower()
    ).strip().lower()

    playwright_headless = 'true'
    playwright_cli_mfa = 'false'
    playwright_remember_me = 'false'
    playwright_mfa_timeout = existing.get('PLAYWRIGHT_MFA_TIMEOUT', 120)
    playwright_login_timeout = existing.get('PLAYWRIGHT_LOGIN_TIMEOUT', 300)
    if use_playwright == 'true':
        playwright_headless = _ask(
            'Run browser in headless mode? (true / false; false lets you complete MFA manually)',
            str(existing.get('PLAYWRIGHT_HEADLESS', True)).lower()
        ).strip().lower()
        playwright_cli_mfa = _ask(
            'Enter MFA codes at the terminal instead of in the browser? (true / false; needed for headless MFA logins)',
            str(existing.get('PLAYWRIGHT_CLI_MFA', False)).lower()
        ).strip().lower()
        playwright_remember_me = _ask(
            'Tick "Keep me logged in" during login to reduce how often you need to log in again? (true / false)',
            str(existing.get('PLAYWRIGHT_REMEMBER_ME', False)).lower()
        ).strip().lower()
        mfa_timeout_input = _ask(
            'Seconds to wait for manual MFA completion when headless=false',
            playwright_mfa_timeout
        ).strip()
        if mfa_timeout_input.isdigit():
            playwright_mfa_timeout = int(mfa_timeout_input)
        login_timeout_input = _ask(
            'Seconds to wait for each login page navigation step',
            playwright_login_timeout
        ).strip()
        if login_timeout_input.isdigit():
            playwright_login_timeout = int(login_timeout_input)

    custom_config = {
        'HOST_URL': jira_host,
        'USER_EMAIL': user,
        'API_TOKEN': password,
        'INCLUDE_ATTACHMENTS': attachments.lower(),
        'DOWNLOAD_LOCALLY': download_locally.lower(),
        'USE_PLAYWRIGHT': use_playwright == "true",
        'PLAYWRIGHT_HEADLESS': playwright_headless != "false",
        'PLAYWRIGHT_CLI_MFA': playwright_cli_mfa == "true",
        'PLAYWRIGHT_REMEMBER_ME': playwright_remember_me == "true",
        'PLAYWRIGHT_MFA_TIMEOUT': playwright_mfa_timeout,
        'PLAYWRIGHT_LOGIN_TIMEOUT': playwright_login_timeout,
        'UPLOAD_TO_S3': {
            'AWS_ENDPOINT_URL': "",
            'AWS_REGION': "",
            'S3_BUCKET': "",
            'S3_DIR': "",
            'AWS_ACCESS_KEY': "",
            'AWS_SECRET_KEY': "",
            'AWS_IS_SECURE': True
        }
    }

    upload_backup = _ask(
        'Do you want to upload the backup file to S3? (true / false)',
        'true' if existing_s3.get('S3_BUCKET') else ''
    )
    if upload_backup.lower() == 'true':
        custom_config['UPLOAD_TO_S3']['AWS_ENDPOINT_URL'] = _ask('What is your AWS endpoint url?', existing_s3.get('AWS_ENDPOINT_URL', ''))
        custom_config['UPLOAD_TO_S3']['AWS_REGION'] = _ask('What is your AWS region?', existing_s3.get('AWS_REGION', ''))
        custom_config['UPLOAD_TO_S3']['S3_BUCKET'] = _ask('What is the S3 bucket name?', existing_s3.get('S3_BUCKET', ''))
        custom_config['UPLOAD_TO_S3']['S3_DIR'] = _ask('What is the S3 directory for upload? (example Atlassian/)', existing_s3.get('S3_DIR', ''))
        custom_config['UPLOAD_TO_S3']['AWS_ACCESS_KEY'] = _ask('What is your AWS access key?', existing_s3.get('AWS_ACCESS_KEY', ''))
        custom_config['UPLOAD_TO_S3']['AWS_SECRET_KEY'] = _ask('What is your AWS secret key?', existing_s3.get('AWS_SECRET_KEY', ''))
        custom_config['UPLOAD_TO_S3']['AWS_IS_SECURE'] = _ask('Do you want to use SSL? (true / false)', existing_s3.get('AWS_IS_SECURE', True))

    with open(config_path, 'w+') as config_file:
        yaml.dump(custom_config, config_file, default_flow_style=False)

    if use_playwright == "true":
        print(
            "\n-> Playwright mode enabled.\n"
            "   Make sure you have installed the Chromium browser:\n"
            "     playwright install chromium\n"
        )


if __name__ == "__main__":
    create_config()
