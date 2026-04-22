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
"""

import json
import time
import os
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
        """
        if self._cookies_file and os.path.exists(self._cookies_file):
            print("-> Saved cookies found, skipping login and navigating directly to backup page")
            return

        self._do_login_flow(page)

    def _do_login_flow(self, page) -> None:
        """Perform the actual login sequence: email → password → MFA."""
        host = self.config["HOST_URL"]
        login_url = f"https://{host}/login"
        print(f"-> Navigating to login page: {login_url}")
        login_timeout_ms = self._login_timeout * 1_000
        page.goto(login_url, wait_until="networkidle", timeout=login_timeout_ms)

        # ---- Check for SSO redirect before we try anything ----
        self._check_for_sso(page)

        # ---- Email field ----
        try:
            email_field = page.get_by_label("Email", exact=False)
            email_field.wait_for(state="visible", timeout=15_000)
            email_field.fill(self.config["USER_EMAIL"])
        except PlaywrightTimeoutError:
            # Some Atlassian tenants present a single combined form
            email_field = page.locator('input[type="email"], input[name="username"]').first
            email_field.fill(self.config["USER_EMAIL"])

        # ---- "Continue" / "Next" button ----
        try:
            page.get_by_role("button", name="Continue").click()
        except PlaywrightTimeoutError:
            page.get_by_role("button", name="Next").click()
        page.wait_for_load_state("networkidle", timeout=login_timeout_ms)

        # ---- After "Continue", check again for SSO ----
        self._check_for_sso(page)

        # ---- Password / API token field ----
        try:
            password_field = page.get_by_label("Password", exact=False)
            password_field.wait_for(state="visible", timeout=15_000)
            password_field.fill(self.config["API_TOKEN"])
        except PlaywrightTimeoutError:
            password_field = page.locator('input[type="password"]').first
            password_field.fill(self.config["API_TOKEN"])

        # ---- Submit ----
        try:
            page.get_by_role("button", name="Log in").click()
        except PlaywrightTimeoutError:
            page.get_by_role("button", name="Sign in").click()

        # Wait for any post-login redirect to settle before MFA check.
        # A timeout here is non-fatal: _handle_mfa polls the URL itself.
        try:
            page.wait_for_load_state("networkidle", timeout=login_timeout_ms)
        except PlaywrightTimeoutError:
            print("-> Warning: page did not reach networkidle after login submit; continuing")

        # ---- MFA detection ----
        self._handle_mfa(page)

        # Ensure the post-login / post-MFA page is fully loaded before continuing
        try:
            page.wait_for_load_state("networkidle", timeout=login_timeout_ms)
        except PlaywrightTimeoutError:
            print("-> Warning: page did not reach networkidle after MFA/login; continuing")

        print("-> Login completed")

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
        """Detect an MFA prompt and either wait (headed) or raise (headless)."""
        # Heuristic: if we are still on a login / verification page after auth
        mfa_indicators = ["verify", "mfa", "two-step", "two-factor", "verification"]
        if any(indicator in page.url.lower() for indicator in mfa_indicators):
            if not self._headless:
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
            else:
                raise RuntimeError(
                    "MFA / two-step verification is required but Playwright is running in "
                    "headless mode.  Set PLAYWRIGHT_HEADLESS: false in config.yaml and retry."
                )

    def _is_auth_redirect(self, url: str) -> bool:
        """Return True when *url* indicates an authentication redirect."""
        url_lower = url.lower()
        auth_indicators = ["atlassian.com/login", "/login", "id.atlassian.com"]
        return any(ind in url_lower for ind in auth_indicators)

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
            print("-> Session expired or not authenticated – logging in fresh")
            self._do_login_flow(page)
            page.goto(backup_page, wait_until="load", timeout=self._login_timeout * 1_000)

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

        self._check_backup_rate_limit(page)
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

        # ---- Capture the existing backup link URL (if any) before clicking ----
        # The page may already show a link from a previous backup run.  We need
        # to wait for a *new* link that differs from the pre-click URL so that
        # we don't accidentally return the stale previous-backup URL.
        existing_href: str = ""
        try:
            existing_locator = page.locator('span#backupLocation a[href]').first
            if existing_locator.is_visible(timeout=3_000):
                existing_href = existing_locator.get_attribute("href") or ""
        except Exception:
            pass

        # ---- Click "Create backup for cloud" (id="submit") ----
        try:
            page.locator('#submit').click(timeout=15_000)
        except Exception:
            # Fallback: match by value attribute
            page.locator('input[value="Create backup for cloud"]').click()

        self._check_backup_rate_limit(page)
        print("-> Backup process started, waiting for download link…")

        # ---- Wait at least _CONFLUENCE_BACKUP_INITIAL_WAIT s before polling so
        #      the server has time to start generating the new backup and
        #      overwrite the old link. ----
        time.sleep(_CONFLUENCE_BACKUP_INITIAL_WAIT)

        # ---- Poll until a *new* backup download link appears ----
        # We retry for up to _CONFLUENCE_BACKUP_LINK_TIMEOUT seconds, checking
        # every _CONFLUENCE_BACKUP_POLL_INTERVAL seconds.
        deadline = time.time() + _CONFLUENCE_BACKUP_LINK_TIMEOUT
        href = ""
        while time.time() < deadline:
            try:
                link_locator = page.locator('span#backupLocation a[href]').first
                if link_locator.is_visible(timeout=5_000):
                    candidate = link_locator.get_attribute("href") or ""
                    if candidate and candidate != existing_href:
                        href = candidate
                        break
            except Exception:
                pass
            time.sleep(_CONFLUENCE_BACKUP_POLL_INTERVAL)

        if not href:
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

        After a backup button is clicked Atlassian may display a message of the
        form::

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
            "approximate time till next allowed backup",
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
