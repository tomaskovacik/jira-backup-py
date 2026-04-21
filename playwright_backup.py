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

The class reads two extra keys from *config*:

* ``PLAYWRIGHT_HEADLESS`` (bool, default ``True``) – run browser headless.
* ``PLAYWRIGHT_MFA_TIMEOUT`` (int, default ``120``) – seconds to wait for
  manual MFA completion when running in headed mode.
"""

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
        # _cookies will be populated after login and reused for HTTP downloads
        self._cookies: list = []

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
        """Launch Chromium and return *(browser, page)*."""
        browser = pw.chromium.launch(headless=self._headless)
        context = browser.new_context()
        page = context.new_page()
        return browser, page

    def _login(self, page) -> None:
        """Authenticate against Atlassian Cloud using email + API token / password."""
        host = self.config["HOST_URL"]
        login_url = f"https://{host}/login"
        print(f"-> Navigating to login page: {login_url}")
        page.goto(login_url, wait_until="networkidle")

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
        page.wait_for_load_state("networkidle")

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

        page.wait_for_load_state("networkidle")

        # ---- MFA detection ----
        self._handle_mfa(page)

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
                while time.time() < deadline:
                    if host in page.url and not any(i in page.url.lower() for i in mfa_indicators):
                        print("-> MFA completed, continuing.")
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

    def _do_jira_backup(self, page) -> str:
        """Navigate to the Jira backup admin page, trigger backup, return URL."""
        host = self.config["HOST_URL"]
        backup_page = f"https://{host}/secure/admin/XmlBackup!default.jspa"
        print(f"-> Navigating to Jira backup page: {backup_page}")
        page.goto(backup_page, wait_until="networkidle")

        # ---- Attachments checkbox ----
        include = str(self.config.get("INCLUDE_ATTACHMENTS", "false")).lower() == "true"
        try:
            checkbox = page.get_by_label("Include attachments", exact=False)
            if checkbox.is_visible():
                if include != checkbox.is_checked():
                    checkbox.click()
        except PlaywrightTimeoutError:
            pass

        # ---- Click "Backup" ----
        page.get_by_role("button", name="Backup").click()
        print("-> Backup process started, waiting for download link…")

        # ---- Wait for download link ----
        download_link = page.locator('a[href*="/plugins/servlet/"]').first
        download_link.wait_for(state="visible", timeout=600_000)  # 10 min
        href = download_link.get_attribute("href")
        if not href.startswith("http"):
            href = f"https://{host}{href}"
        print(f"-> Backup ready: {href}")
        return href

    def _do_confluence_backup(self, page) -> str:
        """Navigate to the Confluence backup admin page, trigger backup, return URL."""
        host = self.config["HOST_URL"]
        backup_page = f"https://{host}/wiki/admin/backup.action"
        print(f"-> Navigating to Confluence backup page: {backup_page}")
        page.goto(backup_page, wait_until="networkidle")

        # ---- Attachments checkbox ----
        include = str(self.config.get("INCLUDE_ATTACHMENTS", "false")).lower() == "true"
        try:
            checkbox = page.get_by_label("Include attachments", exact=False)
            if checkbox.is_visible():
                if include != checkbox.is_checked():
                    checkbox.click()
        except PlaywrightTimeoutError:
            pass

        # ---- Click "Backup" ----
        page.get_by_role("button", name="Backup").click()
        print("-> Backup process started, waiting for download link…")

        # ---- Poll until a download link appears ----
        download_link = page.locator('a[href*="/wiki/download/"]').first
        download_link.wait_for(state="visible", timeout=600_000)  # 10 min
        href = download_link.get_attribute("href")
        if not href.startswith("http"):
            href = f"https://{host}{href}"
        print(f"-> Backup ready: {href}")
        return href

    def _save_cookies(self, page) -> None:
        """Persist browser cookies so the requests session can reuse the auth."""
        try:
            self._cookies = page.context.cookies()
        except Exception:
            self._cookies = []

    def _inject_cookies_into_session(self) -> None:
        """Transfer cookies from the browser context into the requests.Session."""
        for cookie in self._cookies:
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
            )
