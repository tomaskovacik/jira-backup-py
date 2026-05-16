# Atlassian MFA / login quirks for Playwright mode

This note captures behavior observed while making Atlassian browser login work
reliably in Playwright mode for Jira and Confluence backups.

## Current login strategy

- Go directly to `https://id.atlassian.com/login`.
- When `PLAYWRIGHT_CLI_MFA: true`, ask for Atlassian login email, browser
  password, and a fresh MFA code in the terminal before the browser starts the
  login flow.
- Use the collected MFA code only when the MFA page is actually reached.
- Support the same CLI-assisted flow in both headed and headless mode.

## Known Atlassian quirks

1. Atlassian's login form is more reliable when the browser goes to
   `https://id.atlassian.com/login` directly instead of relying on a product
   page redirect to `/login`.
2. The email step, password step, and MFA step use different pages and can keep
   the browser on `id.atlassian.com` even when the flow is progressing normally.
3. A button click on the password step may not advance the flow even when the
   password field was filled correctly. Retrying submit with `Enter` in the
   password field improved reliability and was required in real-world testing.
4. MFA codes are time-sensitive. Prompting for a fresh code before browser login
   gives the automation the full validity window for the later MFA submit.
5. After successful MFA, Atlassian may land on `home.atlassian.com` before the
   script navigates back to the Jira or Confluence backup page.

## Selector details captured from the real Atlassian pages

- Email field: `input[data-testid='username']`, `input[id^='username-']`
- Password field: `input[data-testid='password']`,
  `input[autocomplete='current-password']`, `input#password`
- MFA field: `input#two-step-verification-otp-code-input`,
  `input[name='otpCode']`
- Remember-me checkbox:
  `input[data-testid='remember-me-checkbox--hidden-checkbox']`,
  `input[name='remember']`

## Useful debug flags

- `PLAYWRIGHT_DEBUG_LOG_INPUTS: true`
  - Prints the collected email, password, and MFA code to stdout before login.
  - Use only temporarily because it exposes secrets in plain text.
- `PLAYWRIGHT_DEBUG_BROWSER_CONSOLE: true`
  - Mirrors browser console messages and page errors to stdout.
  - Helpful for seeing whether the flow reached password submit, MFA, and the
    final authenticated redirect.

## Noise that looked scary but was not the blocker

During successful runs, Atlassian pages still emitted many browser-console
messages such as:

- CSP report-only warnings
- FedCM / Google identity warnings
- feature-gate warnings
- transient 400/404/412/429 frontend requests
- occasional page-level JavaScript errors on Atlassian pages

Those messages were noisy but did not prevent successful login as long as the
flow advanced from password submit to MFA and then away from Atlassian auth
pages.
