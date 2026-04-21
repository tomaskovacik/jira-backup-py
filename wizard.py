import os
import yaml


def create_config():
    jira_host = input("What is your Jira host name? ")
    user = input("What is your Jira account email address? ")
    password = input("Paste your Jira API token: ")
    attachments = input("Do you want to include attachments? (true / false) ")
    download_locally = input("Do you want to download the backup file locally? (true / false) ")

    use_playwright = input("Do you want to use Playwright web UI mode instead of REST API? (true / false) ").strip().lower()

    playwright_headless = "true"
    playwright_mfa_timeout = 120
    if use_playwright == "true":
        playwright_headless = input(
            "Run browser in headless mode? (true / false) "
            "[false lets you complete MFA manually] "
        ).strip().lower()
        mfa_timeout_input = input(
            "Seconds to wait for manual MFA completion when headless=false (default 120): "
        ).strip()
        if mfa_timeout_input.isdigit():
            playwright_mfa_timeout = int(mfa_timeout_input)

    custom_config = {
        'HOST_URL': jira_host,
        'USER_EMAIL': user,
        'API_TOKEN': password,
        'INCLUDE_ATTACHMENTS': attachments.lower(),
        'DOWNLOAD_LOCALLY': download_locally.lower(),
        'USE_PLAYWRIGHT': use_playwright == "true",
        'PLAYWRIGHT_HEADLESS': playwright_headless != "false",
        'PLAYWRIGHT_MFA_TIMEOUT': playwright_mfa_timeout,
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

    upload_backup = input("Do you want to upload the backup file to S3? (true / false) ")
    if upload_backup.lower() == 'true':
        custom_config['UPLOAD_TO_S3']['AWS_ENDPOINT_URL'] = input("What is your AWS endpoint url? ")
        custom_config['UPLOAD_TO_S3']['AWS_REGION'] = input("What is your AWS region? ")
        custom_config['UPLOAD_TO_S3']['S3_BUCKET'] = input("What is the S3 bucket name? ")
        custom_config['UPLOAD_TO_S3']['S3_DIR'] = input("What is the S3 directory for upload? (example Atlassian/) ")
        custom_config['UPLOAD_TO_S3']['AWS_ACCESS_KEY'] = input("What is your AWS access key? ")
        custom_config['UPLOAD_TO_S3']['AWS_SECRET_KEY'] = input("What is your AWS secret key? ")
        custom_config['UPLOAD_TO_S3']['AWS_IS_SECURE'] = input("Do you want to use SSL? (true / false) ")

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
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
