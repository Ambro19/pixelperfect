# tests/suite/api/test_email_rate_limits.py
# =============================================================================
# Release H (F-20): forgot-password and forgot-username cannot be used to
# flood an inbox or exhaust the email quota, and the limits never reveal
# whether an address has an account.
# =============================================================================
import abuse_limits


def _post(client, path, email, ip=None):
    headers = {"CF-Connecting-IP": ip} if ip else {}
    return client.post(path, json={"email": email}, headers=headers)


def test_same_address_gets_at_most_three_reset_emails_an_hour(client, make_user, sent_mail):
    make_user(email="target@example.com")
    for i in range(6):
        r = _post(client, "/auth/forgot-password", "target@example.com", ip=f"10.0.0.{i}")
        assert r.status_code == 200 and r.json() == {"ok": True}   # same answer every time
    assert len([m for m in sent_mail if m["kind"] == "reset"]) == abuse_limits.EMAIL_LIMIT


def test_address_limit_ignores_capitals(client, make_user, sent_mail):
    make_user(email="target@example.com")
    for i, e in enumerate(["target@example.com", "TARGET@example.com", "Target@Example.com", "target@EXAMPLE.com"]):
        _post(client, "/auth/forgot-password", e, ip=f"10.0.1.{i}")
    assert len(sent_mail) <= abuse_limits.EMAIL_LIMIT


def test_one_ip_is_cut_off_with_429_and_retry_after(client, make_user, sent_mail):
    codes = [_post(client, "/auth/forgot-password", f"u{i}@example.com", ip="203.0.113.7").status_code
             for i in range(abuse_limits.IP_LIMIT + 3)]
    assert codes[:abuse_limits.IP_LIMIT] == [200] * abuse_limits.IP_LIMIT
    assert set(codes[abuse_limits.IP_LIMIT:]) == {429}
    r = _post(client, "/auth/forgot-password", "x@example.com", ip="203.0.113.7")
    assert int(r.headers["retry-after"]) > 0


def test_ip_limit_is_shared_by_both_email_endpoints(client):
    for i in range(abuse_limits.IP_LIMIT):
        path = "/auth/forgot-password" if i % 2 else "/auth/forgot-username"
        assert _post(client, path, f"v{i}@example.com", ip="198.51.100.9").status_code == 200
    assert _post(client, "/auth/forgot-username", "z@example.com", ip="198.51.100.9").status_code == 429


def test_other_ips_are_not_affected(client):
    for i in range(abuse_limits.IP_LIMIT + 1):
        _post(client, "/auth/forgot-password", f"w{i}@example.com", ip="192.0.2.1")
    assert _post(client, "/auth/forgot-password", "w@example.com", ip="192.0.2.2").status_code == 200


def test_a_forged_x_forwarded_for_does_not_dodge_the_limit(client):
    # Render passes client-supplied X-Forwarded-For through, so it is ignored;
    # Cloudflare's CF-Connecting-IP (which clients cannot set) decides.
    codes = [client.post("/auth/forgot-password", json={"email": f"f{i}@example.com"},
                         headers={"X-Forwarded-For": f"1.2.3.{i}", "CF-Connecting-IP": "203.0.113.50"}
                         ).status_code
             for i in range(abuse_limits.IP_LIMIT + 1)]
    assert codes[-1] == 429


def test_true_client_ip_is_used_when_cf_connecting_ip_is_absent(client):
    for i in range(abuse_limits.IP_LIMIT):
        client.post("/auth/forgot-password", json={"email": f"t{i}@example.com"},
                    headers={"True-Client-IP": "203.0.113.60"})
    r = client.post("/auth/forgot-password", json={"email": "t@example.com"},
                    headers={"True-Client-IP": "203.0.113.60"})
    assert r.status_code == 429


def test_username_reminders_are_limited_per_address(client, make_user, sent_mail):
    make_user(email="name@example.com")
    for i in range(6):
        assert _post(client, "/auth/forgot-username", "name@example.com", ip=f"10.9.0.{i}").json() == {"ok": True}
    assert len([m for m in sent_mail if m["kind"] == "username"]) == abuse_limits.EMAIL_LIMIT


def test_unknown_and_known_addresses_get_identical_answers_when_limited(client, make_user, sent_mail):
    make_user(email="known@example.com")
    for i in range(5):
        a = _post(client, "/auth/forgot-password", "known@example.com", ip=f"10.8.0.{i}")
        b = _post(client, "/auth/forgot-password", "nobody@example.com", ip=f"10.8.1.{i}")
        assert (a.status_code, a.json()) == (b.status_code, b.json())


def test_window_expires(monkeypatch):
    now = [1000.0]
    lim = abuse_limits.SlidingWindowLimiter(2, 60, clock=lambda: now[0])
    assert lim.hit("k")[0] and lim.hit("k")[0]
    allowed, wait = lim.hit("k")
    assert not allowed and 0 < wait <= 61
    now[0] += 61
    assert lim.hit("k")[0]


def test_contact_form_is_limited_per_ip(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "_send_email_sync", lambda *a: None)
    body = {"name": "N", "email": "n@example.com", "subject": "s", "message": "m"}
    codes = [client.post("/contact", json=body, headers={"CF-Connecting-IP": "203.0.113.20"}).status_code
             for _ in range(abuse_limits.CONTACT_LIMIT + 1)]
    assert codes[:-1] == [200] * abuse_limits.CONTACT_LIMIT and codes[-1] == 429
    other = client.post("/contact", json=body, headers={"CF-Connecting-IP": "203.0.113.21"})
    assert other.status_code == 200
