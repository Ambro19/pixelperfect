# tests/suite/unit/test_mailer.py
# =============================================================================
# Release H: every email goes through mailer.send_email. Resend's HTTP API
# when RESEND_API_KEY is set; Gmail SMTP when only SMTP is configured; a clear
# error when neither is. No real network: requests.post and smtplib are faked.
# =============================================================================
import logging

import pytest
import requests

import mailer


class _Resp:
    def __init__(self, status, text='{"id":"em_1"}'):
        self.status_code, self.text = status, text


@pytest.fixture
def no_provider(monkeypatch):
    for k in ("RESEND_API_KEY", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_REPLY_TO"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def resend(monkeypatch, no_provider):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRETKEY123")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return calls_response[0]
    calls_response = [_Resp(200)]
    monkeypatch.setattr(mailer.requests, "post", fake_post)
    return calls, calls_response


def test_provider_order(monkeypatch, no_provider):
    assert mailer.provider() == "none"
    monkeypatch.setenv("SMTP_USERNAME", "u"); monkeypatch.setenv("SMTP_PASSWORD", "p")
    assert mailer.provider() == "smtp"
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    assert mailer.provider() == "resend"          # Resend wins when both exist


def test_resend_request_shape(resend):
    calls, _ = resend
    assert mailer.send_email("a@example.com", "Hi", "text", "<p>html</p>") == "resend"
    c = calls[0]
    assert c["url"] == "https://api.resend.com/emails"
    assert c["headers"]["Authorization"] == "Bearer re_test_SECRETKEY123"
    assert c["timeout"] == mailer.RESEND_TIMEOUT_S
    assert c["json"] == {
        "from": "PixelPerfect <noreply@pixelperfectapi.net>",
        "to": ["a@example.com"], "subject": "Hi", "text": "text", "html": "<p>html</p>",
        "reply_to": "support@pixelperfectapi.net",
    }


def test_from_and_reply_to_are_configurable(resend, monkeypatch):
    calls, _ = resend
    monkeypatch.setenv("EMAIL_FROM", "PP <hello@pixelperfectapi.net>")
    mailer.send_email("a@example.com", "s", "t", "h", reply_to="visitor@example.com")
    assert calls[0]["json"]["from"] == "PP <hello@pixelperfectapi.net>"
    assert calls[0]["json"]["reply_to"] == "visitor@example.com"


def test_resend_rejection_raises_without_the_key(resend):
    _, resp = resend
    resp[0] = _Resp(403, '{"message":"The pixelperfectapi.net domain is not verified"}')
    with pytest.raises(mailer.MailError) as e:
        mailer.send_email("a@example.com", "s", "t", "h")
    assert "403" in str(e.value) and "not verified" in str(e.value)
    assert "SECRETKEY" not in str(e.value)


def test_resend_unreachable_raises_mail_error(monkeypatch, no_provider):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_SECRETKEY123")

    def boom(*a, **k):
        raise requests.ConnectionError("connect to api.resend.com failed (key re_test_SECRETKEY123)")
    monkeypatch.setattr(mailer.requests, "post", boom)
    with pytest.raises(mailer.MailError) as e:
        mailer.send_email("a@example.com", "s", "t", "h")
    assert "SECRETKEY" not in str(e.value)


def test_smtp_fallback_used_without_resend(monkeypatch, no_provider):
    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, u, p): sent.append(("login", u))
        def sendmail(self, frm, to, body): sent.append(("send", to, body))
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    assert mailer.send_email("a@example.com", "Hi", "t", "h") == "smtp"
    assert sent[1][1] == "a@example.com"
    assert "Reply-To: support@pixelperfectapi.net" in sent[1][2]


def test_nothing_configured_raises(no_provider):
    with pytest.raises(mailer.MailNotConfigured):
        mailer.send_email("a@example.com", "s", "t", "h")


def test_reset_link_never_logged_in_production(monkeypatch, no_provider, caplog):
    import email_utils
    monkeypatch.setenv("ENVIRONMENT", "production")
    with caplog.at_level(logging.DEBUG):
        email_utils.send_password_reset_email("a@example.com", "https://x/reset?token=SECRET-TOKEN")
    assert "SECRET-TOKEN" not in caplog.text


def test_reset_link_logged_in_development(monkeypatch, no_provider, caplog):
    import email_utils
    monkeypatch.setenv("ENVIRONMENT", "development")
    with caplog.at_level(logging.DEBUG):
        email_utils.send_password_reset_email("a@example.com", "https://x/reset?token=DEV-TOKEN")
    assert "DEV-TOKEN" in caplog.text


def test_password_reset_goes_through_resend(resend):
    import email_utils
    calls, _ = resend
    email_utils.send_password_reset_email("a@example.com", "https://pixelperfectapi.net/reset?token=T")
    assert calls[0]["json"]["to"] == ["a@example.com"]
    assert "https://pixelperfectapi.net/reset?token=T" in calls[0]["json"]["html"]


def test_contact_form_goes_through_resend_and_escapes_html(resend, monkeypatch):
    import main
    calls, _ = resend
    monkeypatch.setenv("CONTACT_RECIPIENT", "inbox@example.com")
    main._send_email_sync("<b>Eve</b>", "eve@example.com", "Hello\r\nBcc: x@y.z",
                          "<script>alert(1)</script>", "general")
    j = calls[0]["json"]
    assert j["to"] == ["inbox@example.com"]
    assert j["reply_to"] == "eve@example.com"
    assert "<script>" not in j["html"] and "&lt;script&gt;" in j["html"]
    assert "\r" not in j["subject"] and "\n" not in j["subject"]


def test_username_reminder_goes_through_resend(resend):
    import main
    calls, _ = resend
    main._send_username_reminder_email("a@example.com", "alice")
    assert calls[0]["json"]["subject"] == "Your PixelPerfect username"
    assert "alice" in calls[0]["json"]["text"]
