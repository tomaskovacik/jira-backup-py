"""Tests for the CLI MFA and Remember-me features in PlaywrightAtlassian."""

import unittest
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instance(extra_config=None):
    """Return a PlaywrightAtlassian instance with Playwright not actually imported."""
    # Patch all heavy optional dependencies so the tests work without the real
    # packages installed.
    _heavy_mocks = {
        "playwright": MagicMock(),
        "playwright.sync_api": MagicMock(),
        "boto3": MagicMock(),
        "boto3.s3": MagicMock(),
        "boto3.s3.transfer": MagicMock(),
        "google": MagicMock(),
        "google.cloud": MagicMock(),
        "google.cloud.storage": MagicMock(),
        "azure": MagicMock(),
        "azure.storage": MagicMock(),
        "azure.storage.blob": MagicMock(),
    }
    with patch.dict("sys.modules", _heavy_mocks):
        import importlib
        import sys
        # Remove cached module so the patched import is used
        for mod in list(sys.modules.keys()):
            if "playwright_backup" in mod or (mod == "backup"):
                del sys.modules[mod]

        import playwright_backup  # noqa: PLC0415

        config = {
            "HOST_URL": "example.atlassian.net",
            "USER_EMAIL": "user@example.com",
            "API_TOKEN": "secret",
            "INCLUDE_ATTACHMENTS": "false",
            "PLAYWRIGHT_HEADLESS": True,
            "PLAYWRIGHT_COOKIES_FILE": "",
        }
        if extra_config:
            config.update(extra_config)
        instance = playwright_backup.PlaywrightAtlassian(config)
        return instance, playwright_backup


# ---------------------------------------------------------------------------
# Tests for _handle_mfa
# ---------------------------------------------------------------------------

class HandleMfaTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def _page(self, url="https://id.atlassian.com/verify"):
        page = MagicMock()
        page.url = url
        return page

    def test_no_mfa_when_url_has_no_mfa_indicator(self):
        """_handle_mfa should do nothing when the URL has no MFA indicator."""
        inst = self._make()
        page = self._page(url="https://example.atlassian.net/secure/admin/CloudExport.jspa")
        # Should not raise
        inst._handle_mfa(page)

    def test_headless_without_cli_mfa_raises(self):
        """Headless mode without CLI MFA raises RuntimeError."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": False})
        page = self._page()
        with self.assertRaises(RuntimeError) as ctx:
            inst._handle_mfa(page)
        self.assertIn("PLAYWRIGHT_CLI_MFA", str(ctx.exception))

    def test_headless_with_cli_mfa_calls_handle_cli_mfa(self):
        """Headless + PLAYWRIGHT_CLI_MFA delegates to _handle_cli_mfa."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        page = self._page()
        inst._handle_cli_mfa = MagicMock()
        inst._handle_mfa(page)
        inst._handle_cli_mfa.assert_called_once_with(page)

    def test_headed_mode_polls_until_url_changes(self):
        """Headed mode waits for the URL to leave MFA indicators."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": False, "PLAYWRIGHT_MFA_TIMEOUT": 10})
        # Simulate URL changing from /verify to the host dashboard on the 2nd call
        urls = ["https://id.atlassian.com/verify", "https://example.atlassian.net/dashboard"]

        page = MagicMock()
        type(page).url = property(lambda self, _iter=iter(urls): next(_iter, urls[-1]))

        with patch("time.sleep"), patch("time.time", side_effect=[0, 1, 5]):
            inst._handle_mfa(page)

    def test_headed_mode_raises_on_timeout(self):
        """Headed mode raises TimeoutError when MFA is not completed in time."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": False, "PLAYWRIGHT_MFA_TIMEOUT": 5})
        page = self._page(url="https://id.atlassian.com/verify")

        # time.time() always returns a value past the deadline
        with patch("time.sleep"), patch("time.time", side_effect=[0, 100]):
            with self.assertRaises(TimeoutError):
                inst._handle_mfa(page)


# ---------------------------------------------------------------------------
# Tests for _handle_cli_mfa
# ---------------------------------------------------------------------------

class HandleCliMfaTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def _page(self, post_submit_url="https://example.atlassian.net/dashboard"):
        page = MagicMock()
        # visible MFA input: autocomplete=one-time-code
        mfa_input = MagicMock()
        mfa_input.is_visible.return_value = True

        def locator_side_effect(selector):
            loc = MagicMock()
            loc.first = mfa_input
            return loc

        page.locator.side_effect = locator_side_effect

        # After submitting, URL changes to a non-MFA page
        type(page).url = property(lambda self: post_submit_url)
        return page, mfa_input

    def test_fills_code_and_clicks_verify(self):
        """_handle_cli_mfa fills the code and clicks the Verify button."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        page, mfa_input = self._page()

        verify_btn = MagicMock()
        verify_btn.is_visible.return_value = True
        page.get_by_role.return_value = verify_btn

        with patch("builtins.input", return_value="123456"):
            inst._handle_cli_mfa(page)

        mfa_input.fill.assert_called_once_with("123456")
        verify_btn.click.assert_called_once()

    def test_empty_code_raises_value_error(self):
        """_handle_cli_mfa raises ValueError when an empty code is entered."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        page, _ = self._page()

        with patch("builtins.input", return_value=""):
            with self.assertRaises(ValueError):
                inst._handle_cli_mfa(page)

    def test_raises_when_mfa_field_not_found(self):
        """_handle_cli_mfa raises RuntimeError when no MFA input is found."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})

        page = MagicMock()
        type(page).url = property(lambda self: "https://id.atlassian.com/verify")

        invisible_input = MagicMock()
        invisible_input.is_visible.return_value = False
        loc = MagicMock()
        loc.first = invisible_input
        page.locator.return_value = loc

        with patch("builtins.input", return_value="123456"):
            with self.assertRaises(RuntimeError) as ctx:
                inst._handle_cli_mfa(page)
        self.assertIn("Could not locate", str(ctx.exception))

    def test_raises_when_still_on_mfa_page_after_submit(self):
        """_handle_cli_mfa raises RuntimeError when page stays on MFA URL after submit."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        page, _ = self._page(post_submit_url="https://id.atlassian.com/verify")

        verify_btn = MagicMock()
        verify_btn.is_visible.return_value = True
        page.get_by_role.return_value = verify_btn

        with patch("builtins.input", return_value="000000"):
            with self.assertRaises(RuntimeError) as ctx:
                inst._handle_cli_mfa(page)
        self.assertIn("incorrect or expired", str(ctx.exception))


# ---------------------------------------------------------------------------
# Tests for _do_login_flow – Remember-me checkbox
# ---------------------------------------------------------------------------

class RememberMeTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def _build_page(self, remember_me_visible=True, remember_me_checked=False):
        """Return a minimal page mock suitable for testing _do_login_flow."""
        page = MagicMock()
        page.url = "https://example.atlassian.net/dashboard"

        # Email field
        email_field = MagicMock()
        email_field.wait_for = MagicMock()

        # Password field
        password_field = MagicMock()
        password_field.wait_for = MagicMock()

        # Remember-me checkbox
        checkbox = MagicMock()
        checkbox.is_visible.return_value = remember_me_visible
        checkbox.is_checked.return_value = remember_me_checked

        page.get_by_label.side_effect = lambda label, **kw: (
            email_field if "email" in label.lower()
            else password_field if "password" in label.lower()
            else checkbox
        )

        # Buttons
        btn = MagicMock()
        btn.is_visible.return_value = False
        page.get_by_role.return_value = btn

        return page, checkbox

    def test_remember_me_checkbox_checked_when_enabled(self):
        """Checkbox is checked when PLAYWRIGHT_REMEMBER_ME is true."""
        inst = self._make({"PLAYWRIGHT_REMEMBER_ME": True})
        page, checkbox = self._build_page(remember_me_visible=True, remember_me_checked=False)

        # Stub out navigation and MFA to focus on checkbox behaviour
        page.goto = MagicMock()
        page.wait_for_load_state = MagicMock()
        inst._check_for_sso = MagicMock()
        inst._handle_mfa = MagicMock()

        inst._do_login_flow(page)

        checkbox.check.assert_called()

    def test_remember_me_checkbox_not_touched_when_disabled(self):
        """Checkbox is not interacted with when PLAYWRIGHT_REMEMBER_ME is false."""
        inst = self._make({"PLAYWRIGHT_REMEMBER_ME": False})
        page, checkbox = self._build_page(remember_me_visible=True, remember_me_checked=False)

        page.goto = MagicMock()
        page.wait_for_load_state = MagicMock()
        inst._check_for_sso = MagicMock()
        inst._handle_mfa = MagicMock()

        inst._do_login_flow(page)

        checkbox.check.assert_not_called()

    def test_remember_me_already_checked_is_skipped(self):
        """Checkbox is not re-checked when it is already in the checked state."""
        inst = self._make({"PLAYWRIGHT_REMEMBER_ME": True})
        page, checkbox = self._build_page(remember_me_visible=True, remember_me_checked=True)

        page.goto = MagicMock()
        page.wait_for_load_state = MagicMock()
        inst._check_for_sso = MagicMock()
        inst._handle_mfa = MagicMock()

        inst._do_login_flow(page)

        checkbox.check.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for auth-redirect guard in _do_jira_backup / _do_confluence_backup
# ---------------------------------------------------------------------------

class AuthRedirectGuardTests(unittest.TestCase):
    """The auth-redirect guard must allow _do_login_flow when CLI MFA is on."""

    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def _page_redirected_then_ok(self, backup_url):
        """Return a page that starts on a login URL then 'loads' the backup page."""
        page = MagicMock()
        # First call: auth redirect; subsequent calls: backup page
        urls = iter(["https://id.atlassian.com/login", backup_url])
        type(page).url = property(lambda self, _it=urls: next(_it, backup_url))
        return page

    def test_jira_headless_cli_mfa_calls_login_flow_on_redirect(self):
        """_do_jira_backup must call _do_login_flow (not raise) when headless+cli_mfa."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        backup_page = f"https://example.atlassian.net/secure/admin/CloudExport.jspa"
        page = self._page_redirected_then_ok(backup_page)

        # Raise a sentinel so we stop immediately after _do_login_flow is called
        class _LoginCalled(Exception):
            pass

        inst._do_login_flow = MagicMock(side_effect=_LoginCalled)

        with self.assertRaises(_LoginCalled):
            inst._do_jira_backup(page)

        inst._do_login_flow.assert_called_once_with(page)

    def test_jira_headless_no_cli_mfa_raises_on_redirect(self):
        """_do_jira_backup must raise when headless and cli_mfa is off."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": False})
        page = MagicMock()
        page.url = "https://id.atlassian.com/login"

        with patch("time.sleep"):
            with self.assertRaises(RuntimeError):
                inst._do_jira_backup(page)

    def test_confluence_headless_cli_mfa_calls_login_flow_on_redirect(self):
        """_do_confluence_backup must call _do_login_flow (not raise) when headless+cli_mfa."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        backup_page = "https://example.atlassian.net/wiki/plugins/servlet/ondemandbackupmanager/admin"
        page = self._page_redirected_then_ok(backup_page)

        # Raise a sentinel so we stop immediately after _do_login_flow is called
        class _LoginCalled(Exception):
            pass

        inst._do_login_flow = MagicMock(side_effect=_LoginCalled)

        with self.assertRaises(_LoginCalled):
            inst._do_confluence_backup(page)

        inst._do_login_flow.assert_called_once_with(page)

    def test_confluence_headless_no_cli_mfa_raises_on_redirect(self):
        """_do_confluence_backup must raise when headless and cli_mfa is off."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": False})
        page = MagicMock()
        page.url = "https://id.atlassian.com/login"

        with self.assertRaises(RuntimeError):
            inst._do_confluence_backup(page)

    def test_login_headless_cli_mfa_calls_login_flow_without_cookies(self):
        """_login must call _do_login_flow when headless+cli_mfa and no cookies file."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True,
                           "PLAYWRIGHT_COOKIES_FILE": ""})
        page = MagicMock()
        inst._do_login_flow = MagicMock()

        inst._login(page)

        inst._do_login_flow.assert_called_once_with(page)

    def test_login_headless_no_cli_mfa_raises_without_cookies(self):
        """_login must raise when headless, cli_mfa off, and no cookies file."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": False,
                           "PLAYWRIGHT_COOKIES_FILE": ""})
        page = MagicMock()

        with self.assertRaises(RuntimeError):
            inst._login(page)


if __name__ == "__main__":
    unittest.main()
