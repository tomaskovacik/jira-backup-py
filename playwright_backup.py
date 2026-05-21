"""
playwright_backup.py – Playwright-based Web UI backup driver for Atlassian Cloud.

This module provides a drop-in replacement for the REST-API-based ``Atlassian``
class in ``backup.py``.  It launches a Chromium browser, authenticates through
the Atlassian Cloud web UI, triggers a backup, waits for it to finish, and
returns the download URL.  All upload / file-handling helpers (download_file,
stream_to_s3, …) are inherited unchanged from the base ``Atlassian`` class so
that the rest of ``backup.py`` does not need to know which backend is being used.

Usage
-----
Instantiate ``PlaywrightAtlassian`` exactly like ``Atlassian`` and call the
same public methods:

    from playwright_backup import PlaywrightAtlassian
    atlass = PlaywrightAtlassian(config)
    url = atlass.create_jira_backup()

The class reads three extra keys from *config*:

* ``PLAYWRIGHT_HEADLESS`` (bool, default ``True``) – run browser headless.
* ``PLAYWRIGHT_MFA_TIMEOUT`` (int, default ``120``) – seconds to wait for
  manual MFA completion when running in headed mode.
* ``PLAYWRIGHT_LOGIN_TIMEOUT`` (int, default ``300``) – seconds to wait for
  each navigation step during login (page load, network idle, etc.).
* ``PLAYWRIGHT_COOKIES_FILE`` (str, default ``"playwright_cookies.json"``) –
  path to a JSON file where browser session cookies are persisted between runs.
  Set to an empty string ``""`` to disable cookie persistence.
* ``PLAYWRIGHT_CLI_MFA`` (bool, default ``False``) – when ``True`` and running
  in headless mode, the script will pause at the MFA / two-step verification
  step and prompt the user to type their TOTP code in the terminal.  The code
  is then filled into the browser automatically.  This allows fully headless,
  automated logins where MFA is required without opening a visible browser.
* ``PLAYWRIGHT_REMEMBER_ME`` (bool, default ``False``) – when ``True`` the
  "Keep me logged in" / "Remember me" checkbox on the Atlassian login page is
  checked before submitting the password form.
"""

import json
import time
import os
import getpass
import requests

from backup import Atlassian

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "playwright is required for Playwright mode. "
        "Install it with:  pip install playwright && playwright install chromium"
    ) from exc


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Seconds to wait after triggering a Confluence backup before polling for the
# new download link.  The page often still shows the *previous* backup's link
# immediately after the button is clicked; this delay gives the server time to
# start generating a new backup and update the link.
_CONFLUENCE_BACKUP_INITIAL_WAIT: int = 30

# Maximum seconds to wait for a new Confluence backup download link to appear.
_CONFLUENCE_BACKUP_LINK_TIMEOUT: int = 600  # 10 minutes

# Seconds between polling attempts when waiting for a new backup link.
_CONFLUENCE_BACKUP_POLL_INTERVAL: int = 5

# Milliseconds to use when quickly checking whether an element is currently
# visible (e.g. optional checkboxes or MFA input fields).  Intentionally
# shorter than the full login timeout to avoid long hangs on elements that
# simply don't exist on the current page.
_QUICK_VISIBILITY_TIMEOUT_MS: int = 3_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SSO_INDICATORS = [
    "okta.com",
    "onelogin.com",
    "auth0.com",
    "microsoft.com/adfs",
    "microsoftonline.com",
    "login.microsoftonline",
    "shibboleth",
    "pingidentity",
    "google.com/accounts",
]


def _is_sso_page(url: str) -> bool:
    """Return True when the current URL looks like a third-party SSO page."""
    url_lower = url.lower()
    return any(indicator in url_lower for indicator in _SSO_INDICATORS)


# ---------------------------------------------------------------------------
# PlaywrightAtlassian
# ---------------------------------------------------------------------------

class PlaywrightAtlassian(Atlassian):
    """Drives the Atlassian Cloud web UI via Playwright to create backups.

    All upload / download helpers are inherited from :class:`~backup.Atlassian`
    so only the backup-creation methods are overridden here.
    """

    def __init__(self, config):
        super().__init__(config)
        self._headless: bool = config.get("PLAYWRIGHT_HEADLESS", True)
        self._mfa_timeout: int = int(config.get("PLAYWRIGHT_MFA_TIMEOUT", 120))
        self._login_timeout: int = int(config.get("PLAYWRIGHT_LOGIN_TIMEOUT", 300))
        # _cookies will be populated after login and reused for HTTP downloads
        self._cookies: list = []
        # Path for persisted session cookies; empty string disables persistence
        _default_cookies_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "playwright_cookies.json"
        )
        self._cookies_file: str = config.get("PLAYWRIGHT_COOKIES_FILE", _default_cookies_file)
        # CLI MFA: prompt for TOTP code at the terminal when running headless
        self._cli_mfa: bool = bool(config.get("PLAYWRIGHT_CLI_MFA", False))
        # Remember-me: tick the "Keep me logged in" checkbox before submitting
        self._remember_me: bool = bool(config.get("PLAYWRIGHT_REMEMBER_ME", False))
        # Debug flags for investigating interactive Atlassian login issues
        self._debug_log_inputs: bool = bool(config.get("PLAYWRIGHT_DEBUG_LOG_INPUTS", False))
        self._debug_browser_console: bool = bool(config.get("PLAYWRIGHT_DEBUG_BROWSER_CONSOLE", False))
        # Terminal-entered credentials/MFA code for the current headless CLI login attempt
        self._cli_login_email: str = ""
        self._cli_login_password: str = ""
        self._cli_mfa_code: str = ""

    # ------------------------------------------------------------------
    # Public API (mirroring Atlassian)
    # ------------------------------------------------------------------

    def create_jira_backup(self) -> str:
        """Trigger a Jira backup via the web UI and return the download URL."""
        with sync_playwright() as pw:
            browser, page = self._launch(pw)
            try:
                self._login(page)
                return self._do_jira_backup(page)
            finally:
                self._save_cookies(page)
                browser.close()

    def create_confluence_backup(self) -> str:
        """Trigger a Confluence backup via the web UI and return the download URL."""
        with sync_playwright() as pw:
            browser, page = self._launch(pw)
            try:
                self._login(page)
                return self._do_confluence_backup(page)
            finally:
                self._save_cookies(page)
                browser.close()

    def download_file(self, url: str, local_filename: str, max_retries: int = 5) -> str:
        """Download *url* locally, reusing the authenticated browser session cookies."""
        self._inject_cookies_into_session()
        return super().download_file(url, local_filename, max_retries)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _launch(self, pw):
        """Launch Chromium and return *(browser, page)*, restoring saved cookies if available."""
        browser = pw.chromium.launch(headless=self._headless)
        context = browser.new_context()
        if self._cookies_file and os.path.exists(self._cookies_file):
            try:
                with open(self._cookies_file, "r") as fh:
                    saved_cookies = json.load(fh)
                context.add_cookies(saved_cookies)
                print(f"-> Loaded saved session cookies from {self._cookies_file}")
            except Exception as exc:
                print(f"-> Could not load saved cookies ({exc}); will log in fresh")
        page = context.new_page()
        if self._debug_browser_console:
            page.on("console", self._handle_browser_console_message)
            page.on("pageerror", self._handle_browser_page_error)
        return browser, page

    def _login(self, page) -> None:
        """Authenticate against Atlassian Cloud using email + API token / password.

        When a saved cookies file is present the login flow is skipped entirely
        (wizard-once mode): the cookies are already loaded by :meth:`_launch`
        and the backup methods navigate directly to the backup page.  If the
        saved session has expired those methods detect the auth redirect and
        call :meth:`_do_login_flow` themselves before retrying.

        On the very first run (no cookies file) a full interactive login is
        performed and the resulting cookies are persisted for future runs.
        If headless mode is active and CLI MFA is disabled, an error is raised
        immediately because MFA cannot be completed non-interactively.
        """
        if self._cookies_file and os.path.exists(self._cookies_file):
            print("-> Saved cookies found, skipping login and navigating directly to backup page")
            return

        if self._headless and not self._cli_mfa:
            self._raise_headless_login_required()

        self._do_login_flow(page)

    def _do_login_flow(self, page) -> None:
        """Perform the actual login sequence: email → password → MFA."""
        self._prepare_cli_login_attempt()
        host = self.config["HOST_URL"]
        login_url = "https://id.atlassian.com/login"
        self._log_debug_inputs()
        print(f"-> Navigating to login page: {login_url}")
        login_timeout_ms = self._login_timeout * 1_000
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=login_timeout_ms)

            # ---- Check for SSO redirect before we try anything ----
            self._check_for_sso(page)

            # ---- Email field ----
            self._fill_login_field(
                page,
                field_name="email",
                value=self._get_login_email(),
                candidates=(
                    ("input[data-testid='username']", page.locator("input[data-testid='username']")),
                    ("input[id^='username-']", page.locator("input[id^='username-']")),
                    ("label:Email", page.get_by_label("Email", exact=False)),
                    ("input#username", page.locator("input#username")),
                    ("input[name='username']", page.locator("input[name='username']")),
                    ("input[name='email']", page.locator("input[name='email']")),
                    ("input[type='email']", page.locator("input[type='email']")),
                ),
            )

            # ---- "Continue" / "Next" button ----
            self._click_login_control(
                page,
                control_name="continue button",
                candidates=(
                    ("button:Continue", page.get_by_role("button", name="Continue")),
                    ("button:Next", page.get_by_role("button", name="Next")),
                    ("button[type='submit']", page.locator("button[type='submit']")),
                ),
            )
            page.wait_for_load_state("domcontentloaded", timeout=login_timeout_ms)
            self._log_debug_page_state(page, "after-continue")

            # ---- After "Continue", check again for SSO ----
            self._check_for_sso(page)

            # ---- Password / API token field ----
            password_field = self._fill_login_field(
                page,
                field_name="password",
                value=self._get_login_password(),
                candidates=(
                    ("input[data-testid='password']", page.locator("input[data-testid='password']")),
                    ("input[autocomplete='current-password']", page.locator("input[autocomplete='current-password']")),
                    ("input[placeholder='Enter password']", page.locator("input[placeholder='Enter password']")),
                    ("input[id^='password-']", page.locator("input[id^='password-']")),
                    ("label:Password", page.get_by_label("Password", exact=False)),
                    ("input#password", page.locator("input#password")),
                    ("input[name='password']", page.locator("input[name='password']")),
                    ("input[type='password']", page.locator("input[type='password']")),
                ),
            )

            # ---- "Keep me logged in" / "Remember me" checkbox ----
            if self._remember_me:
                for locator in (
                    page.locator('input[data-testid="remember-me-checkbox--hidden-checkbox"]'),
                    page.locator('input[name="remember"]'),
                    page.get_by_label("Keep me logged in", exact=False),
                    page.get_by_label("Remember me", exact=False),
                    page.locator('input[name="rememberMe"], input[id*="remember"], input[id*="keep-me"]'),
                ):
                    try:
                        if locator.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS) and not locator.is_checked():
                            locator.check()
                            print("-> 'Keep me logged in' checkbox checked.")
                            break
                    except Exception:
                        continue

            # ---- Submit ----
            self._submit_password_form(page, password_field, login_timeout_ms)
            self._log_debug_page_state(page, "after-login-submit")

            # ---- MFA detection ----
            self._handle_mfa(page)
            self._log_debug_page_state(page, "after-mfa")

            # Ensure the post-login / post-MFA page is fully loaded before continuing
            try:
                page.wait_for_load_state("domcontentloaded", timeout=login_timeout_ms)
            except PlaywrightTimeoutError:
                print("-> Warning: page did not reach domcontentloaded after MFA/login; continuing")
            self._log_debug_page_state(page, "before-login-complete")

            if self._is_auth_redirect(page.url):
                diagnostics = self._describe_auth_page(page)
                diagnostics_suffix = ""
                if diagnostics:
                    diagnostics_suffix = "\nObserved page state:\n- " + "\n- ".join(diagnostics)
                raise RuntimeError(
                    "Login form submission completed but Atlassian still appears to be on an "
                    "authentication page.\n"
                    f"Current URL: {page.url}\n"
                    "This usually means the credentials were rejected, an additional challenge "
                    "appeared, or Atlassian did not accept the automated form submission."
                    f"{diagnostics_suffix}"
                )

            print("-> Login completed")
        finally:
            self._clear_cli_login_attempt()

    def _prepare_cli_login_attempt(self) -> None:
        """Collect terminal credentials up front for a CLI-assisted login attempt."""
        if not self._cli_mfa:
            return

        configured_email = str(self.config.get("USER_EMAIL", "")).strip()
        print(
            "-> CLI-assisted login: enter your Atlassian email, password, and a fresh MFA code\n"
            "   before Playwright submits the login form."
        )
        email_prompt = "-> Enter Atlassian login email"
        if configured_email:
            email_prompt += f" [{configured_email}]"
        email_prompt += ": "
        entered_email = input(email_prompt).strip()
        self._cli_login_email = entered_email or configured_email
        if not self._cli_login_email:
            raise ValueError("No Atlassian login email was entered; aborting login.")

        self._cli_login_password = getpass.getpass("-> Enter Atlassian login password: ").strip()
        if not self._cli_login_password:
            raise ValueError("No Atlassian login password was entered; aborting login.")

        print(
            "-> Wait until your authenticator app shows a fresh MFA code, then type it below.\n"
            "   Playwright will submit that code as soon as the verification step appears."
        )
        self._cli_mfa_code = input("-> Enter MFA code: ").strip()
        if not self._cli_mfa_code:
            raise ValueError("No MFA code was entered; aborting login.")

    def _clear_cli_login_attempt(self) -> None:
        """Discard any cached terminal input after the login attempt finishes."""
        self._cli_login_email = ""
        self._cli_login_password = ""
        self._cli_mfa_code = ""

    def _get_login_email(self) -> str:
        """Return the email address to use for the current browser login attempt."""
        if self._cli_login_email:
            return self._cli_login_email
        return self.config["USER_EMAIL"]

    def _get_login_password(self) -> str:
        """Return the secret to use for the current browser login attempt."""
        if self._cli_login_password:
            return self._cli_login_password
        return self.config["API_TOKEN"]

    def _log_debug_inputs(self) -> None:
        """Print collected login inputs when explicit debug logging is enabled."""
        if not self._debug_log_inputs:
            return

        print("-> DEBUG login inputs follow (contains sensitive data)")
        print(f"-> DEBUG email: {self._get_login_email()!r}")
        print(f"-> DEBUG password: {self._get_login_password()!r}")
        if self._cli_mfa_code:
            print(f"-> DEBUG mfa_code: {self._cli_mfa_code!r}")

    def _handle_browser_console_message(self, message) -> None:
        """Mirror browser console messages into the app log when debug is enabled."""
        try:
            text = message.text()
        except TypeError:
            text = message.text
        msg_type = getattr(message, "type", None)
        if callable(msg_type):
            msg_type = msg_type()
        print(f"-> BROWSER CONSOLE [{msg_type or 'log'}] {text}")

    def _handle_browser_page_error(self, error) -> None:
        """Mirror uncaught browser page errors into the app log."""
        print(f"-> BROWSER PAGE ERROR {error}")

    def _log_debug_page_state(self, page, label: str) -> None:
        """Print the current page URL/title when debug browser logging is enabled."""
        if not self._debug_browser_console:
            return
        try:
            title = page.title()
        except Exception:
            title = "<unavailable>"
        print(f"-> DEBUG PAGE STATE [{label}] url={page.url!r} title={title!r}")

    def _fill_login_field(self, page, field_name: str, value: str, candidates):
        """Fill the first visible login field candidate and verify the value sticks."""
        for candidate_name, locator in candidates:
            try:
                field = locator.first
                field.wait_for(state="visible", timeout=_QUICK_VISIBILITY_TIMEOUT_MS)
                if not field.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS):
                    continue
                field.scroll_into_view_if_needed()
                field.click(force=True)
                self._clear_login_field(field)
                self._type_login_value(page, field, value)
                self._finalize_login_field(field)
                actual_value = field.input_value()
                if actual_value != value:
                    field.fill(value, force=True)
                    self._finalize_login_field(field)
                    actual_value = field.input_value()
                if actual_value != value:
                    field.evaluate(
                        """(element, nextValue) => {
                            element.focus();
                            element.value = '';
                            element.dispatchEvent(new Event('input', { bubbles: true }));
                            element.value = nextValue;
                            element.dispatchEvent(new Event('input', { bubbles: true }));
                            element.dispatchEvent(new Event('change', { bubbles: true }));
                        }""",
                        value,
                    )
                    self._finalize_login_field(field)
                    actual_value = field.input_value()
                if actual_value != value:
                    continue
                print(f"-> Filled {field_name} field via {candidate_name}")
                return field
            except Exception:
                continue

        raise RuntimeError(
            f"Could not fill the {field_name} field on the Atlassian login page.\n"
            f"Current URL: {page.url}"
        )

    def _clear_login_field(self, field) -> None:
        """Clear the active login field before typing."""
        try:
            field.press("Control+A")
            field.press("Backspace")
        except Exception:
            pass
        try:
            field.fill("", force=True)
        except Exception:
            pass

    def _type_login_value(self, page, field, value: str) -> None:
        """Prefer real keystroke-style entry for Atlassian login fields."""
        for method_name in ("press_sequentially", "type"):
            try:
                getattr(field, method_name)(value, delay=120)
                return
            except Exception:
                continue
        try:
            page.keyboard.type(value, delay=120)
        except Exception:
            pass

    def _finalize_login_field(self, field) -> None:
        """Trigger blur/validation after entering a login field value."""
        try:
            field.press("Tab")
        except Exception:
            pass
        time.sleep(0.2)

    def _submit_password_form(self, page, password_field, login_timeout_ms: int) -> None:
        """Submit the password step, retrying once with Enter if Atlassian ignores the click."""
        self._click_login_control(
            page,
            control_name="login button",
            candidates=(
                ("button:Log in", page.get_by_role("button", name="Log in")),
                ("button:Sign in", page.get_by_role("button", name="Sign in")),
                ("button[type='submit']", page.locator("button[type='submit']")),
            ),
        )
        self._wait_for_login_transition(page, login_timeout_ms, stage="login submit")
        if not self._is_auth_redirect(page.url):
            return
        if not self._is_password_step_visible(page):
            return
        try:
            password_field.press("Enter")
            print("-> Login button did not advance; retried submit via Enter in password field.")
        except Exception:
            return
        self._wait_for_login_transition(page, login_timeout_ms, stage="login retry")

    def _wait_for_login_transition(self, page, login_timeout_ms: int, stage: str) -> None:
        """Wait briefly for Atlassian's login UI to react to a submit action."""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=login_timeout_ms)
        except PlaywrightTimeoutError:
            print(f"-> Warning: page did not reach domcontentloaded after {stage}; continuing")
        time.sleep(0.5)

    def _click_login_control(self, page, control_name: str, candidates) -> None:
        """Click the first visible login control candidate."""
        for candidate_name, locator in candidates:
            try:
                control = locator.first
                if not control.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS):
                    continue
                if hasattr(control, "is_enabled") and not control.is_enabled():
                    continue
                control.click()
                print(f"-> Clicked {control_name} via {candidate_name}")
                return
            except Exception:
                continue

        raise RuntimeError(
            f"Could not find the {control_name} on the Atlassian login page.\n"
            f"Current URL: {page.url}"
        )

    def _is_password_step_visible(self, page) -> bool:
        """Return True when the password form still appears to be on screen."""
        for selector in (
            "input[data-testid='password']",
            "input[autocomplete='current-password']",
            "input#password",
            "input[name='password']",
            "input[type='password']",
        ):
            try:
                if page.locator(selector).first.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS):
                    return True
            except Exception:
                continue
        return False

    def _describe_auth_page(self, page) -> list[str]:
        """Return short human-readable diagnostics for the current Atlassian auth page."""
        diagnostics: list[str] = []
        if self._is_password_step_visible(page):
            diagnostics.append("Password field is still visible after submit.")
        if self._is_any_selector_visible(
            page,
            (
                "input#two-step-verification-otp-code-input",
                'input[name="otpCode"]',
                'input[autocomplete="one-time-code"]',
            ),
        ):
            diagnostics.append("MFA input is visible on the page.")
        if self._is_any_selector_visible(
            page,
            (
                'iframe[src*="recaptcha"]',
                'div[id*="captcha"]',
                '[data-testid*="captcha"]',
            ),
        ):
            diagnostics.append("A captcha or bot challenge appears to be present.")
        for selector in (
            '[role="alert"]',
            '[aria-live="assertive"]',
            '[aria-live="polite"]',
            '[data-testid*="error"]',
            '[data-testid*="alert"]',
            '[id*="error"]',
        ):
            message = self._read_visible_text(page, selector)
            if message:
                diagnostics.append(f"Visible page message: {message}")
                break
        return diagnostics

    def _is_any_selector_visible(self, page, selectors) -> bool:
        """Return True when any selector resolves to a visible element."""
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS):
                    return True
            except Exception:
                continue
        return False

    def _read_visible_text(self, page, selector: str) -> str:
        """Return normalized text for the first visible element matched by selector."""
        try:
            element = page.locator(selector).first
            if not element.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS):
                return ""
            text = element.inner_text(timeout=_QUICK_VISIBILITY_TIMEOUT_MS)
        except Exception:
            return ""
        return " ".join(text.split())[:300]

    def _check_for_sso(self, page) -> None:
        """Raise an informative error when the browser lands on a third-party SSO page."""
        if _is_sso_page(page.url):
            raise RuntimeError(
                f"Playwright mode does not support third-party SSO login.\n"
                f"Current URL: {page.url}\n"
                f"Please use the standard REST API mode (without --playwright) or configure "
                f"your Atlassian account to allow API-token authentication."
            )

    def _handle_mfa(self, page) -> None:
        """Detect an MFA prompt and handle it based on the configured mode.

        Three strategies are available:
        1. **CLI-assisted MFA** (``PLAYWRIGHT_CLI_MFA: true``) – prompt the user
           to enter their TOTP code at the terminal; fill it into the browser's
           MFA input field automatically and submit.
        2. **Headed mode without CLI MFA** – wait for the human to complete MFA
           in the browser window within ``PLAYWRIGHT_MFA_TIMEOUT`` seconds.
        3. **Headless without CLI MFA** – raise an error.
        """
        mfa_indicators = ["verify", "mfa", "two-step", "two-factor", "verification"]
        if not any(indicator in page.url.lower() for indicator in mfa_indicators):
            return

        if self._cli_mfa:
            self._handle_cli_mfa(page)
            return

        if not self._headless:
            # Headed mode: user completes MFA interactively in the browser window
            print(
                f"-> MFA / two-step verification detected.\n"
                f"   Complete it in the browser window within {self._mfa_timeout} seconds."
            )
            deadline = time.time() + self._mfa_timeout
            host = self.config["HOST_URL"]
            login_timeout_ms = self._login_timeout * 1_000
            while time.time() < deadline:
                if host in page.url and not any(i in page.url.lower() for i in mfa_indicators):
                    print("-> MFA completed, continuing.")
                    try:
                        page.wait_for_load_state("networkidle", timeout=login_timeout_ms)
                    except PlaywrightTimeoutError:
                        print("-> Warning: page did not reach networkidle after MFA; continuing")
                    return
                time.sleep(2)
            raise TimeoutError(
                f"MFA was not completed within {self._mfa_timeout} seconds."
            )

        raise RuntimeError(
            "MFA / two-step verification is required but Playwright is running in "
            "headless mode.  Options:\n"
            "  1. Set PLAYWRIGHT_CLI_MFA: true in config.yaml to enter your TOTP code "
            "at the terminal (headless mode).\n"
            "  2. Set PLAYWRIGHT_HEADLESS: false in config.yaml to complete MFA in a "
            "browser window."
        )

    def _handle_cli_mfa(self, page) -> None:
        """Prompt the user for their TOTP code, fill it in the browser, and submit.

        Called when ``PLAYWRIGHT_CLI_MFA`` is enabled and an MFA page is
        detected.  The code is valid for ~30 seconds so the user is instructed
        to retrieve a *fresh* code just before typing it.
        """
        print(
            "-> MFA / two-step verification detected (CLI-assisted mode).\n"
            "   Using the MFA code collected in the terminal before browser login."
        )
        mfa_code = self._cli_mfa_code or input("-> Enter MFA code: ").strip()
        if not mfa_code:
            raise ValueError("No MFA code was entered; aborting login.")

        login_timeout_ms = self._login_timeout * 1_000

        # Try a sequence of selectors that cover Atlassian's MFA input variants
        mfa_input_selectors = [
            'input#two-step-verification-otp-code-input',
            'input[name="otpCode"]',
            'input[id^="two-step-verification-"]',
            'input[autocomplete="one-time-code"]',
            'input[name="code"]',
            'input[name="pin"]',
            'input[placeholder*="code" i]',
            'input[placeholder*="digit" i]',
            'input[type="tel"]',
            'input[type="number"]',
            'input[type="text"][maxlength]',
        ]
        mfa_field = None
        for selector in mfa_input_selectors:
            try:
                candidate = page.locator(selector).first
                if candidate.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS):
                    mfa_field = candidate
                    break
            except Exception:
                continue

        if mfa_field is None:
            raise RuntimeError(
                "Could not locate the MFA code input field on the page.\n"
                f"Current URL: {page.url}\n"
                "Please report this as a bug or switch to headed mode "
                "(PLAYWRIGHT_HEADLESS: false) and complete MFA manually."
            )

        mfa_field.fill(mfa_code)

        # Submit: try known button labels then fall back to the first submit button
        submitted = False
        for button_name in ("Verify", "Continue", "Submit", "Log in", "Sign in"):
            try:
                btn = page.get_by_role("button", name=button_name)
                if btn.is_visible(timeout=_QUICK_VISIBILITY_TIMEOUT_MS):
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue
        if not submitted:
            try:
                page.locator('button[type="submit"], input[type="submit"]').first.click()
                submitted = True
            except Exception:
                pass
        if not submitted:
            # Last resort: press Enter in the input field
            mfa_field.press("Enter")

        print("-> MFA code submitted, waiting for redirect…")
        try:
            page.wait_for_load_state("networkidle", timeout=login_timeout_ms)
        except PlaywrightTimeoutError:
            print("-> Warning: page did not reach networkidle after MFA submit; continuing")

        mfa_indicators = ["verify", "mfa", "two-step", "two-factor", "verification"]
        if any(ind in page.url.lower() for ind in mfa_indicators):
            raise RuntimeError(
                f"MFA submission did not redirect away from the verification page.\n"
                f"Current URL: {page.url}\n"
                "The code may have been incorrect or expired. "
                "Please re-run the script and try again with a fresh code."
            )

        print("-> MFA completed via CLI input.")

    def _is_auth_redirect(self, url: str) -> bool:
        """Return True when *url* indicates an authentication redirect."""
        url_lower = url.lower()
        auth_indicators = ["atlassian.com/login", "/login", "id.atlassian.com"]
        return any(ind in url_lower for ind in auth_indicators)

    def _raise_headless_login_required(self) -> None:
        """Raise a clear error when headless mode needs an interactive login (e.g. MFA)."""
        raise RuntimeError(
            "A fresh login is required but Playwright is running in headless mode.\n"
            "Options:\n"
            "  1. Set PLAYWRIGHT_CLI_MFA: true in config.yaml to enter your TOTP code "
            "at the terminal while the browser runs headlessly.\n"
            "  2. Set PLAYWRIGHT_HEADLESS: false in config.yaml to complete MFA in a "
            "browser window."
        )

    def _do_jira_backup(self, page) -> str:
        """Navigate to the Jira Cloud Export admin page, trigger backup, return URL."""
        host = self.config["HOST_URL"]
        backup_page = f"https://{host}/secure/admin/CloudExport.jspa"
        print(f"-> Navigating to Jira Cloud Export page: {backup_page}")
        try:
            page.goto(backup_page, wait_until="load", timeout=self._login_timeout * 1_000)
        except PlaywrightTimeoutError:
            print("-> Warning: backup page timed out waiting for load; continuing")

        # If we were redirected to a login page (e.g. saved cookies expired),
        # perform a fresh login and navigate to the backup page again.
        if self._is_auth_redirect(page.url):
            if self._headless and not self._cli_mfa:
                self._raise_headless_login_required()
            print("-> Session expired or not authenticated – logging in fresh")
            self._do_login_flow(page)
            page.goto(backup_page, wait_until="load", timeout=self._login_timeout * 1_000)

        # ---- Wait for the page JS to finish rendering ----
        # The Jira export page renders the previous backup download link via
        # JavaScript *after* the initial HTML load event fires.  Give it 10 s to
        # appear before we try to read it, otherwise we may capture an empty link
        # and lose the fallback URL we need when the site is rate-limited.
        print("-> Waiting 10 s for page to render existing backup link…")
        time.sleep(10)

        # ---- Pre-click: read any existing backup link already on the page ----
        # We always capture this before touching the button so we can fall back to
        # it both for CHECK_EXISTING_BACKUP and for rate-limit recovery below.
        pre_click_href: str = ""
        try:
            pre_click_locator = page.locator('a[href*="/plugins/servlet/export/"]').first
            if pre_click_locator.is_visible(timeout=10_000):
                pre_click_href = pre_click_locator.get_attribute("href") or ""
                if pre_click_href:
                    print(f"-> Existing backup link found on page: {pre_click_href}")
        except Exception:
            pass

        # ---- Pre-click: check for a rate-limit message already on the page ----
        # Atlassian shows the rate-limit banner as soon as the export page loads
        # when a recent backup already exists; we must handle it before clicking.
        try:
            self._check_backup_rate_limit(page, wait_ms=0)
        except RuntimeError:
            # Page is already rate-limited – use the existing link if available.
            if pre_click_href:
                full_href = pre_click_href if pre_click_href.startswith("http") else f"https://{host}{pre_click_href}"
                if not self.is_already_downloaded(full_href):
                    print(f"-> Found existing Jira backup not yet downloaded locally: {full_href}")
                    print("-> Using existing backup instead of creating a new one.")
                    return full_href
                else:
                    print(f"-> Existing backup {full_href} was already downloaded previously, skipping.")
            # The Jira export page does not always render a visible download link
            # when rate-limited.  Fall back to the REST API to locate the last backup.
            api_url = self.get_existing_jira_backup()
            if api_url:
                print(f"-> Found existing Jira backup via REST API: {api_url}")
                print("-> Using existing backup instead of creating a new one.")
                return api_url
            else:
                print("-> No existing backup found via REST API either; re-raising rate limit error.")
            raise

        # ---- Check for an existing backup we haven't downloaded yet ----
        # If CHECK_EXISTING_BACKUP is enabled and there is already a download link
        # on the page pointing to a backup UUID we don't have locally, return that
        # URL instead of triggering a new backup (covers the case where someone
        # manually created a backup via the web UI).
        if self.config.get("CHECK_EXISTING_BACKUP", False) and pre_click_href:
            full_href = pre_click_href if pre_click_href.startswith("http") else f"https://{host}{pre_click_href}"
            if not self.is_already_downloaded(full_href):
                print(f"-> Found existing Jira backup not yet downloaded locally: {full_href}")
                print("-> Skipping new backup creation and using existing backup.")
                return full_href

        # ---- Attachments checkbox ----
        include = str(self.config.get("INCLUDE_ATTACHMENTS", "false")).lower() == "true"
        try:
            checkbox = page.get_by_label("Include attachments", exact=False)
            if checkbox.is_visible():
                if include != checkbox.is_checked():
                    checkbox.click()
        except PlaywrightTimeoutError:
            pass

        # ---- Click the export / backup button ----
        for button_name in ("Backup", "Start backup", "Export", "Submit"):
            try:
                btn = page.get_by_role("button", name=button_name)
                btn.click()
                break
            except Exception:
                continue
        else:
            # Fallback: first submit button on the page
            page.locator('input[type="submit"], button[type="submit"]').first.click()

        try:
            self._check_backup_rate_limit(page)
        except RuntimeError:
            if pre_click_href:
                full_href = pre_click_href if pre_click_href.startswith("http") else f"https://{host}{pre_click_href}"
                if not self.is_already_downloaded(full_href):
                    print(f"-> Found existing Jira backup not yet downloaded locally: {full_href}")
                    print("-> Using existing backup instead of creating a new one.")
                    return full_href
                else:
                    print(f"-> Existing backup {full_href} was already downloaded previously, skipping.")
            api_url = self.get_existing_jira_backup()
            if api_url:
                print(f"-> Found existing Jira backup via REST API: {api_url}")
                print("-> Using existing backup instead of creating a new one.")
                return api_url
            else:
                print("-> No existing backup found via REST API either; re-raising rate limit error.")
            raise

        print("-> Backup process started, waiting for download link…")

        # ---- Wait for download link to become visible ----
        # Use a specific selector for the export servlet to avoid matching unrelated
        # /plugins/servlet/* links (e.g. /plugins/servlet/webhooks) that appear on
        # the same page before the real backup download link is ready.
        download_link = page.locator('a[href*="/plugins/servlet/export/"]').first
        download_link.wait_for(state="visible", timeout=600_000)  # 10 min
        href = download_link.get_attribute("href")
        if not href.startswith("http"):
            href = f"https://{host}{href}"
        # Sanity-check: the href must look like an actual download, not an admin page.
        if not (href.endswith(".zip") or "fileId" in href or "export/download" in href):
            raise RuntimeError(
                f"Unexpected backup URL detected (possible page-layout mismatch): {href}\n"
                "The selector matched a non-backup link. Please report this issue."
            )
        print(f"-> Backup ready: {href}")
        return href

    def _do_confluence_backup(self, page) -> str:
        """Navigate to the Confluence Cloud backup admin page, trigger backup, return URL."""
        host = self.config["HOST_URL"]
        backup_page = f"https://{host}/wiki/plugins/servlet/ondemandbackupmanager/admin"
        print(f"-> Navigating to Confluence backup page: {backup_page}")
        try:
            page.goto(backup_page, wait_until="load", timeout=self._login_timeout * 1_000)
        except PlaywrightTimeoutError:
            print("-> Warning: backup page timed out waiting for load; continuing")

        # If we were redirected to a login page (e.g. saved cookies expired),
        # perform a fresh login and navigate to the backup page again.
        if self._is_auth_redirect(page.url):
            if self._headless and not self._cli_mfa:
                self._raise_headless_login_required()
            print("-> Session expired or not authenticated – logging in fresh")
            self._do_login_flow(page)
            page.goto(backup_page, wait_until="load", timeout=self._login_timeout * 1_000)

        # ---- Attachments checkbox ----
        # Confluence Cloud uses "cbAttachments2" as the checkbox name on the
        # ondemandbackupmanager page (there are two attachment checkboxes; the
        # relevant one for cloud backups has name="cbAttachments2").
        include = str(self.config.get("INCLUDE_ATTACHMENTS", "false")).lower() == "true"
        try:
            checkbox = page.locator('input[name="cbAttachments2"]')
            if checkbox.is_visible():
                if include != checkbox.is_checked():
                    checkbox.click()
        except Exception:
            pass

        # ---- Wait for the page JS to finish rendering ----
        # The Confluence backup page renders the previous backup download link via
        # JavaScript *after* the initial HTML load event fires.  Give it 10 s to
        # appear before we try to read it, otherwise we may capture an empty link
        # and lose the fallback URL we need when the site is rate-limited.
        print("-> Waiting 10 s for page to render existing backup link…")
        time.sleep(10)

        # ---- Capture the existing backup link URL (if any) before clicking ----
        # The page may already show a link from a previous backup run.  We need
        # to wait for a *new* link that differs from the pre-click URL so that
        # we don't accidentally return the stale previous-backup URL.
        existing_href: str = ""
        try:
            existing_locator = page.locator('a[href*="/wiki/download/temp/"]').first
            existing_href = existing_locator.get_attribute("href") or ""
            print(f"-> Existing backup link found on page: {existing_href}")
        except Exception:
            pass

        # ---- Pre-click: check for a rate-limit message already on the page ----
        # Atlassian shows the rate-limit banner on page load when a recent backup
        # exists; handle it here so we don't needlessly click the button.
        try:
            self._check_backup_rate_limit(page, wait_ms=0)
        except RuntimeError:
            if existing_href:
                full_existing_href = existing_href if existing_href.startswith("http") else f"https://{host}{existing_href}"
                if not self.is_already_downloaded(full_existing_href):
                    print(f"-> Found existing Confluence backup not yet downloaded locally: {full_existing_href}")
                    print("-> Using existing backup instead of creating a new one.")
                    return full_existing_href
                else:
                    print(f"-> Existing backup {full_existing_href} was already downloaded previously, skipping.")
            # The Confluence backup page may not render a visible download link when
            # rate-limited.  Fall back to the REST API to locate the last backup.
            api_url = self.get_existing_confluence_backup()
            if api_url:
                print(f"-> Found existing Confluence backup via REST API: {api_url}")
                print("-> Using existing backup instead of creating a new one.")
                return api_url
            else:
                print("-> No existing backup found via REST API either; re-raising rate limit error.")
            raise

        # ---- Check for an existing backup we haven't downloaded yet ----
        # If CHECK_EXISTING_BACKUP is enabled and the page already shows a download
        # link pointing to a backup UUID we don't have locally, return that URL
        # instead of triggering a new backup (covers the case where someone manually
        # created a backup via the web UI).
        if self.config.get("CHECK_EXISTING_BACKUP", False) and existing_href:
            full_existing_href = existing_href if existing_href.startswith("http") else f"https://{host}{existing_href}"
            if not self.is_already_downloaded(full_existing_href):
                print(f"-> Found existing Confluence backup not yet downloaded locally: {full_existing_href}")
                print("-> Skipping new backup creation and using existing backup.")
                return full_existing_href

        # ---- Dismiss any Atlassian spotlight/onboarding overlay ----
        # Atlassian sometimes shows a tour/spotlight dialog whose footer div
        # sits on top of the backup button and intercepts pointer events.
        # Try pressing Escape or clicking any "OK"/"Got it"/"Close" button to
        # clear the overlay before we attempt the backup click.
        try:
            spotlight = page.locator('[data-testid="spotlight--dialog-footer"]')
            if spotlight.is_visible(timeout=2_000):
                # Try dismiss buttons in the footer first
                for label in ("OK", "Got it", "Close", "Dismiss", "Next", "Done"):
                    btn = spotlight.locator(f'button:has-text("{label}")')
                    if btn.count() > 0:
                        btn.first.click(timeout=3_000)
                        break
                else:
                    page.keyboard.press("Escape")
                page.wait_for_timeout(500)
        except Exception:
            pass

        # ---- Click "Create backup for cloud" (id="submit") ----
        try:
            page.locator('#submit').click(timeout=15_000)
        except Exception:
            # Fallback: match by value attribute
            page.locator('input[value="Create backup for cloud"]').click()

        try:
            self._check_backup_rate_limit(page)
        except RuntimeError:
            if existing_href:
                full_existing_href = existing_href if existing_href.startswith("http") else f"https://{host}{existing_href}"
                if not self.is_already_downloaded(full_existing_href):
                    print(f"-> Found existing Confluence backup not yet downloaded locally: {full_existing_href}")
                    print("-> Using existing backup instead of creating a new one.")
                    return full_existing_href
                else:
                    print(f"-> Existing backup {full_existing_href} was already downloaded previously, skipping.")
            api_url = self.get_existing_confluence_backup()
            if api_url:
                print(f"-> Found existing Confluence backup via REST API: {api_url}")
                print("-> Using existing backup instead of creating a new one.")
                return api_url
            else:
                print("-> No existing backup found via REST API either; re-raising rate limit error.")
            raise

        print("-> Backup process started, waiting for download link…")

        # ---- Wait at least _CONFLUENCE_BACKUP_INITIAL_WAIT s before polling so
        #      the server has time to start generating the new backup and
        #      overwrite the old link. ----
        time.sleep(_CONFLUENCE_BACKUP_INITIAL_WAIT)

        # ---- Poll until a *new* backup download link appears ----
        # We retry for up to _CONFLUENCE_BACKUP_LINK_TIMEOUT seconds, checking
        # every _CONFLUENCE_BACKUP_POLL_INTERVAL seconds.
        # Note: some Confluence instances render the finished link as
        # <a>Site_Backup.zip</a> with no href attribute.  We therefore match
        # 'span#backupLocation a' (without [href]) and fall back to the REST
        # API when the link is visible but carries no href.
        deadline = time.time() + _CONFLUENCE_BACKUP_LINK_TIMEOUT
        href = ""
        while time.time() < deadline:
            try:
                link_locator = page.locator('span#backupLocation a').first
                if link_locator.is_visible(timeout=5_000):
                    candidate = link_locator.get_attribute("href") or ""
                    if candidate and candidate != existing_href:
                        href = candidate
                        break
                    # Link is visible but has no href – backup may be ready;
                    # try the REST API which returns the filename once complete.
                    api_url = self.get_existing_confluence_backup()
                    if api_url:
                        print(f"-> Backup link visible but has no href; obtained URL via REST API: {api_url}")
                        href = api_url
                        break
            except Exception:
                pass
            time.sleep(_CONFLUENCE_BACKUP_POLL_INTERVAL)

        if not href:
            # One final REST API attempt before giving up entirely.
            api_url = self.get_existing_confluence_backup()
            if api_url:
                print(f"-> Backup link not found on page; obtained URL via REST API: {api_url}")
                href = api_url
            else:
                raise TimeoutError(
                    f"Confluence backup did not produce a new download link within "
                    f"{_CONFLUENCE_BACKUP_LINK_TIMEOUT} seconds."
                )

        if not href.startswith("http"):
            href = f"https://{host}{href}"
        print(f"-> Backup ready: {href}")
        return href

    def _check_backup_rate_limit(self, page, wait_ms: int = 3_000) -> None:
        """Detect and surface the Atlassian backup-frequency rate-limit message.

        Can be called either immediately after page load (pass ``wait_ms=0``) to
        detect a rate-limit banner that Atlassian renders without any user action,
        or after a backup button is clicked (default ``wait_ms=3000``) to give the
        page time to render the banner.

        The message looks like::

            Sorry
            Backup frequency is limited. You can not make another backup right
            now. Approximate time till next allowed backup: HH hours and MM minutes

        When that message is found, its text is printed to the terminal and a
        :class:`RuntimeError` is raised so the caller exits cleanly without
        waiting unnecessarily for a download link that will never appear.
        """
        # Give the page a moment to render any error banner before we check.
        time.sleep(wait_ms / 1_000)

        rate_limit_keywords = [
            "sorry",
            "backup frequency is limited",
            "you can not make another backup",
            "you cannot make another backup",
            "approximate time till next allowed backup",
            "approximate time until next allowed backup",
        ]

        try:
            page_text = page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return  # If we can't read the page, proceed and let the caller handle it

        page_text_lower = page_text.lower()
        # Require at least one of the more specific keywords to avoid false positives
        # on generic "Sorry" messages unrelated to backup rate limiting.
        specific_keywords = rate_limit_keywords[1:]
        if any(kw in page_text_lower for kw in specific_keywords):
            # Try to extract the exact message paragraph for a clean terminal output.
            message_lines = []
            for line in page_text.splitlines():
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                line_lower = line_stripped.lower()
                if any(kw in line_lower for kw in rate_limit_keywords):
                    message_lines.append(line_stripped)
            message = "\n".join(message_lines) if message_lines else "Backup frequency is limited."
            print(f"-> Rate limit message from site:\n{message}")
            raise RuntimeError(message)

    def _save_cookies(self, page) -> None:
        """Persist browser cookies to memory and optionally to disk for session reuse."""
        try:
            self._cookies = page.context.cookies()
        except Exception:
            self._cookies = []
            return

        if not self._cookies_file:
            return

        try:
            with open(self._cookies_file, "w") as fh:
                json.dump(self._cookies, fh)
            print(f"-> Session cookies saved to {self._cookies_file}")
        except Exception as exc:
            print(f"-> Warning: could not save session cookies ({exc})")

    def _inject_cookies_into_session(self) -> None:
        """Transfer cookies from the browser context into the requests.Session."""
        for cookie in self._cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
            )
