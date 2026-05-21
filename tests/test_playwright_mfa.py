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


def _mock_input_field():
    """Return a mock field that behaves like a visible, fillable Playwright input."""
    field = MagicMock()
    field.is_visible.return_value = True
    field._value = ""

    def fill_side_effect(value, **_kwargs):
        field._value = value

    def press_side_effect(key, **_kwargs):
        if key in ("Control+A", "Meta+A", "Backspace", "Delete"):
            field._value = ""
        elif key == "Tab":
            return None

    def type_side_effect(value, **_kwargs):
        field._value = value

    field.fill.side_effect = fill_side_effect
    field.press.side_effect = press_side_effect
    field.type.side_effect = type_side_effect
    field.press_sequentially.side_effect = type_side_effect
    field.input_value.side_effect = lambda: field._value
    field.evaluate.side_effect = lambda _script, value: fill_side_effect(value)
    return field


def _mock_locator(target):
    """Return a Playwright-like locator whose .first resolves to *target*."""
    locator = MagicMock()
    locator.first = target
    return locator


def _mock_hidden_element():
    """Return a mock element that is present but not visible."""
    element = MagicMock()
    element.is_visible.return_value = False
    element.inner_text.return_value = ""
    return element


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

    def test_headed_mode_with_cli_mfa_calls_handle_cli_mfa(self):
        """Headed mode uses CLI MFA when PLAYWRIGHT_CLI_MFA is enabled."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": False, "PLAYWRIGHT_CLI_MFA": True})
        page = self._page()
        inst._handle_cli_mfa = MagicMock()

        inst._handle_mfa(page)

        inst._handle_cli_mfa.assert_called_once_with(page)


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
        inst._cli_mfa_code = "123456"

        verify_btn = MagicMock()
        verify_btn.is_visible.return_value = True
        page.get_by_role.return_value = verify_btn

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
        email_field = _mock_input_field()

        # Password field
        password_field = _mock_input_field()

        # Remember-me checkbox
        checkbox = MagicMock()
        checkbox.is_visible.return_value = remember_me_visible
        checkbox.is_checked.return_value = remember_me_checked

        page.get_by_label.side_effect = lambda label, **kw: (
            _mock_locator(email_field) if "email" in label.lower()
            else _mock_locator(password_field) if "password" in label.lower()
            else checkbox
        )

        # Buttons
        btn = MagicMock()
        btn.is_visible.return_value = True
        btn.is_enabled.return_value = True
        page.get_by_role.return_value = _mock_locator(btn)

        def locator_side_effect(selector):
            if "submit" in selector:
                return _mock_locator(btn)
            return _mock_locator(MagicMock())

        page.locator.side_effect = locator_side_effect

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


class HeadlessCliMfaLoginGuardTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def test_login_raises_in_headless_mode_without_cli_mfa(self):
        """_login raises when a fresh headless login cannot prompt for MFA."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": False})
        page = MagicMock()

        with patch("os.path.exists", return_value=False):
            with self.assertRaises(RuntimeError):
                inst._login(page)

    def test_login_allows_headless_mode_with_cli_mfa(self):
        """_login delegates to _do_login_flow when CLI MFA is enabled."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        page = MagicMock()
        inst._do_login_flow = MagicMock()

        with patch("os.path.exists", return_value=False):
            inst._login(page)

        inst._do_login_flow.assert_called_once_with(page)


class HeadlessCliPromptTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def test_prepare_cli_login_attempt_collects_terminal_inputs(self):
        """CLI MFA prompts for email, password, and MFA code before login."""
        inst = self._make(
            {
                "PLAYWRIGHT_HEADLESS": False,
                "PLAYWRIGHT_CLI_MFA": True,
                "USER_EMAIL": "configured@example.com",
            }
        )

        with patch("builtins.input", side_effect=["", "654321"]), patch(
            "playwright_backup.getpass.getpass",
            return_value="browser-password",
        ):
            inst._prepare_cli_login_attempt()

        self.assertEqual(inst._cli_login_email, "configured@example.com")
        self.assertEqual(inst._cli_login_password, "browser-password")
        self.assertEqual(inst._cli_mfa_code, "654321")

    def test_prepare_cli_login_attempt_rejects_missing_password(self):
        """CLI MFA requires a terminal password before login starts."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": False, "PLAYWRIGHT_CLI_MFA": True})

        with patch("builtins.input", side_effect=["user@example.com"]), patch(
            "playwright_backup.getpass.getpass",
            return_value="",
        ):
            with self.assertRaises(ValueError) as ctx:
                inst._prepare_cli_login_attempt()

        self.assertIn("password", str(ctx.exception).lower())

    def test_do_login_flow_uses_prompted_email_and_password(self):
        """_do_login_flow fills the prompted browser credentials instead of config values."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": False, "PLAYWRIGHT_CLI_MFA": True})
        page = MagicMock()
        page.url = "https://example.atlassian.net/dashboard"

        email_field = _mock_input_field()
        password_field = _mock_input_field()
        remember_me = MagicMock()
        remember_me.is_visible.return_value = False

        page.get_by_label.side_effect = lambda label, **kw: (
            _mock_locator(email_field) if "email" in label.lower()
            else _mock_locator(password_field) if "password" in label.lower()
            else remember_me
        )

        button = MagicMock()
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
        page.get_by_role.return_value = _mock_locator(button)
        page.goto = MagicMock()
        page.wait_for_load_state = MagicMock()
        page.locator.side_effect = lambda selector: _mock_locator(button if "submit" in selector else MagicMock())

        inst._check_for_sso = MagicMock()
        inst._handle_mfa = MagicMock()

        with patch("builtins.input", side_effect=["prompted@example.com", "123456"]), patch(
            "playwright_backup.getpass.getpass",
            return_value="prompted-password",
        ):
            inst._do_login_flow(page)

        self.assertEqual(email_field._value, "prompted@example.com")
        self.assertEqual(password_field._value, "prompted-password")
        self.assertEqual(inst._cli_login_email, "")
        self.assertEqual(inst._cli_login_password, "")
        self.assertEqual(inst._cli_mfa_code, "")


class LoginFieldFallbackTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def test_fill_login_field_uses_js_fallback_when_fill_does_not_stick(self):
        """_fill_login_field falls back to JS value injection when normal fill is ignored."""
        inst = self._make()
        page = MagicMock()
        page.url = "https://id.atlassian.com/login"
        field = _mock_input_field()

        def ignore_fill(_value, **_kwargs):
            return None

        def ignore_type(_value, **_kwargs):
            return None

        field.fill.side_effect = ignore_fill
        field.type.side_effect = ignore_type
        field.press_sequentially.side_effect = ignore_type
        field.evaluate.side_effect = lambda _script, value: setattr(field, "_value", value)

        inst._fill_login_field(
            page,
            field_name="email",
            value="user@example.com",
            candidates=(("input[data-testid='username']", _mock_locator(field)),),
        )

        self.assertEqual(field._value, "user@example.com")
        field.evaluate.assert_called()


class LoginSubmitTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def test_submit_password_form_retries_with_enter_when_password_step_remains_visible(self):
        """Password submit retries with Enter when the click leaves Atlassian on the password page."""
        inst = self._make()
        page = MagicMock()
        page.url = "https://id.atlassian.com/login?email=user%40example.com"
        password_field = _mock_input_field()
        button = MagicMock()
        button.is_visible.return_value = True
        button.is_enabled.return_value = True

        page.get_by_role.return_value = _mock_locator(button)

        def locator_side_effect(selector):
            if selector == "button[type='submit']":
                return _mock_locator(button)
            if "password" in selector:
                return _mock_locator(password_field)
            return _mock_locator(_mock_hidden_element())

        page.locator.side_effect = locator_side_effect
        inst._wait_for_login_transition = MagicMock()

        inst._submit_password_form(page, password_field, 10_000)

        button.click.assert_called_once()
        password_field.press.assert_any_call("Enter")
        self.assertEqual(inst._wait_for_login_transition.call_count, 2)

    def test_describe_auth_page_reports_visible_hints(self):
        """Auth-page diagnostics include password, captcha, and visible error text."""
        inst = self._make()
        page = MagicMock()

        password_field = _mock_input_field()
        captcha = MagicMock()
        captcha.is_visible.return_value = True
        error = MagicMock()
        error.is_visible.return_value = True
        error.inner_text.return_value = "Incorrect email and/or password."

        def locator_side_effect(selector):
            if "password" in selector:
                return _mock_locator(password_field)
            if "captcha" in selector:
                return _mock_locator(captcha)
            if selector == '[role="alert"]':
                return _mock_locator(error)
            return _mock_locator(_mock_hidden_element())

        page.locator.side_effect = locator_side_effect

        diagnostics = inst._describe_auth_page(page)

        self.assertIn("Password field is still visible after submit.", diagnostics)
        self.assertIn("A captcha or bot challenge appears to be present.", diagnostics)
        self.assertIn("Visible page message: Incorrect email and/or password.", diagnostics)


class DebugLoggingTests(unittest.TestCase):
    def _make(self, extra_config=None):
        instance, _ = _make_instance(extra_config)
        return instance

    def test_log_debug_inputs_prints_sensitive_values_when_enabled(self):
        """_log_debug_inputs emits collected login inputs when debug logging is enabled."""
        inst = self._make({"PLAYWRIGHT_DEBUG_LOG_INPUTS": True})
        inst._cli_login_email = "user@example.com"
        inst._cli_login_password = "browser-password"
        inst._cli_mfa_code = "123456"

        with patch("builtins.print") as mock_print:
            inst._log_debug_inputs()

        mock_print.assert_any_call("-> DEBUG login inputs follow (contains sensitive data)")
        mock_print.assert_any_call("-> DEBUG email: 'user@example.com'")
        mock_print.assert_any_call("-> DEBUG password: 'browser-password'")
        mock_print.assert_any_call("-> DEBUG mfa_code: '123456'")

    def test_launch_registers_browser_console_handlers_when_enabled(self):
        """_launch wires browser console and pageerror handlers when debug is enabled."""
        inst = self._make({"PLAYWRIGHT_DEBUG_BROWSER_CONSOLE": True})
        pw = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        pw.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page

        inst._launch(pw)

        page.on.assert_any_call("console", inst._handle_browser_console_message)
        page.on.assert_any_call("pageerror", inst._handle_browser_page_error)

    def test_jira_backup_raises_in_headless_mode_without_cli_mfa(self):
        """_do_jira_backup still rejects fresh headless auth without CLI MFA."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": False})
        page = MagicMock()
        page.url = "https://id.atlassian.com/login"

        with self.assertRaises(RuntimeError):
            inst._do_jira_backup(page)

    def test_jira_backup_allows_headless_mode_with_cli_mfa(self):
        """_do_jira_backup re-authenticates in headless mode when CLI MFA is enabled."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        page = MagicMock()
        page.url = "https://id.atlassian.com/login"
        inst._do_login_flow = MagicMock(side_effect=RuntimeError("sentinel"))

        with self.assertRaisesRegex(RuntimeError, "sentinel"):
            inst._do_jira_backup(page)

        inst._do_login_flow.assert_called_once_with(page)

    def test_confluence_backup_raises_in_headless_mode_without_cli_mfa(self):
        """_do_confluence_backup still rejects fresh headless auth without CLI MFA."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": False})
        page = MagicMock()
        page.url = "https://id.atlassian.com/login"

        with self.assertRaises(RuntimeError):
            inst._do_confluence_backup(page)

    def test_confluence_backup_allows_headless_mode_with_cli_mfa(self):
        """_do_confluence_backup re-authenticates in headless mode when CLI MFA is enabled."""
        inst = self._make({"PLAYWRIGHT_HEADLESS": True, "PLAYWRIGHT_CLI_MFA": True})
        page = MagicMock()
        page.url = "https://id.atlassian.com/login"
        inst._do_login_flow = MagicMock(side_effect=RuntimeError("sentinel"))

        with self.assertRaisesRegex(RuntimeError, "sentinel"):
            inst._do_confluence_backup(page)

        inst._do_login_flow.assert_called_once_with(page)

if __name__ == "__main__":
    unittest.main()
