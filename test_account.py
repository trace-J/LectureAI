"""Tests for account.py: claiming this Mac into a Syllabus account.

The account service is replaced by a scripted fake transport, so nothing here
reaches the network, and the account file lands in a throwaway home. From the
project root:

    .venv/bin/python test_account.py
"""
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _test_home import fresh_home  # noqa: E402

HOME = fresh_home()  # before config is imported

from intake import account, config, doctor  # noqa: E402


def run(label, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {label}\n      {exc}")
        return False
    except Exception as exc:
        print(f"FAIL  {label}\n      unexpected {type(exc).__name__}: {exc}")
        return False
    print(f"ok    {label}")
    return True


results = []
SERVICE = "https://accounts.test"


class FakeService:
    """Answers the calls account.py makes, in the order a real one would."""

    def __init__(self):
        self.calls = []
        self.approved = False
        self.polls_before_answer = 0
        self.fail_polls = 0        # raise on this many polls first (network down)
        self.expired = False
        self.token_ok = True

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        assert url.startswith(SERVICE + "/"), url
        path = url[len(SERVICE):]
        if path == "/device/start":
            return 200, {"device_code": "dc-secret", "user_code": "WXYZ-2345",
                         "verification_uri": SERVICE + "/device",
                         "verification_uri_complete": SERVICE + "/device?code=WXYZ-2345",
                         "expires_in": 900, "interval": 5}
        if path == "/device/poll":
            assert body == {"device_code": "dc-secret"}, body
            if self.fail_polls:
                self.fail_polls -= 1
                raise ConnectionError("no route to host")
            if self.expired:
                return 400, {"error": "expired_token"}
            if self.polls_before_answer:
                self.polls_before_answer -= 1
                return 400, {"error": "authorization_pending", "interval": 5}
            return 200, {"token": "syd_abc", "account": {"id": "a1", "email": "me@example.com", "name": "Me"},
                         "device": {"id": "d1", "name": body and "Kitchen iMac", "profile": "syllabus"}}
        if path == "/me":
            if headers.get("Authorization") != "Bearer syd_abc" or not self.token_ok:
                return 401, {"error": "invalid_token"}
            return 200, {"account": {"id": "a1", "email": "me@example.com"}, "device": {"id": "d1"}}
        if path == "/device/revoke":
            assert headers.get("Authorization") == "Bearer syd_abc", headers
            return 200, {"ok": True}
        raise AssertionError(f"unexpected call {method} {path}")


def reset(service=None):
    account.forget()
    account.cancel_claim()
    account._set_claim(error="")
    config.ACCOUNTS_URL = SERVICE
    account.transport = service or FakeService()
    return account.transport


def t1():
    reset()
    assert account.enabled()
    assert account.summary() == {"enabled": True, "url": SERVICE, "signed_in": False}, account.summary()
    for word in ("off", "OFF", "none", "0", "", "  "):
        config.ACCOUNTS_URL = word
        assert not account.enabled(), word
        assert account.summary() == {"enabled": False, "signed_in": False}
        assert doctor.check_account() is None, "no doctor line when accounts are off"
    config.ACCOUNTS_URL = SERVICE + "/"
    assert account.url() == SERVICE, "a trailing slash is tolerated"
results.append(run("accounts are on by default and ACCOUNTS_URL=off hides them", t1))


def t2():
    reset()
    acct = account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Kitchen iMac",
                           "syllabus", SERVICE, "2026-09-12T17:00:00")
    account.save(acct)
    assert config.ACCOUNT_FILE.exists()
    assert stat.S_IMODE(config.ACCOUNT_FILE.stat().st_mode) == 0o600, "the token file is private"
    assert account.load() == acct
    public = acct.public()
    assert "token" not in public and public["email"] == "me@example.com", public
    assert account.summary()["signed_in"] and account.summary()["device_name"] == "Kitchen iMac"
    check = doctor.check_account()
    assert check.ok and "me@example.com" in check.detail and "Kitchen iMac" in check.detail, check
    config.ACCOUNT_FILE.write_text("not json")
    assert account.load() is None, "a broken file is nobody, not a crash"
    account.forget()
    assert not config.ACCOUNT_FILE.exists()
    assert doctor.check_account().detail.startswith("not signed in"), doctor.check_account()
results.append(run("the account file round-trips, is private, and never leaks the token", t2))


def t3():
    service = reset()
    service.polls_before_answer = 2
    started = account.start_claim("Kitchen iMac", background=False)
    assert started["user_code"] == "WXYZ-2345" and started["running"], started
    assert started["verification_uri_complete"].endswith("code=WXYZ-2345")
    assert service.calls[0][3] == {"name": "Kitchen iMac", "profile": "syllabus"}, service.calls[0]
    status = account.claim_status()
    assert status["running"] and 890 < status["expires_in"] <= 900, status

    slept = []
    acct = account.run_claim(started["device_code"], "Kitchen iMac", started["interval"],
                             status["expires_at"], sleep=slept.append)
    assert acct is not None and acct.email == "me@example.com" and acct.token == "syd_abc", acct
    assert acct.device_name == "Kitchen iMac" and acct.url == SERVICE
    assert slept == [5, 5, 5], slept
    assert len([c for c in service.calls if c[1].endswith("/device/poll")]) == 3
    assert account.load() == acct, "the account is saved once approved"
    assert not account.claim_status()["running"]
results.append(run("a claim shows a code, polls while pending, and saves the account on approval", t3))


def t4():
    service = reset()
    service.fail_polls = 2
    started = account.start_claim(background=False)
    acct = account.run_claim(started["device_code"], started["name"], 5,
                             account.claim_status()["expires_at"], sleep=lambda s: None)
    assert acct is not None, "network trouble is retried, not treated as a refusal"
    assert len([c for c in service.calls if c[1].endswith("/device/poll")]) == 3
    assert started["name"], "a device name is always sent, this Mac's by default"
results.append(run("polling survives the service being briefly unreachable", t4))


def t5():
    service = reset()
    service.expired = True
    started = account.start_claim(background=False)
    acct = account.run_claim(started["device_code"], "x", 5, account.claim_status()["expires_at"],
                             sleep=lambda s: None)
    assert acct is None and account.load() is None
    status = account.claim_status()
    assert not status["running"] and "expired" in status["error"], status

    # The clock running out locally says the same thing without another poll.
    service = reset()
    service.polls_before_answer = 99
    started = account.start_claim(background=False)
    clock = [0.0]

    def now():
        clock[0] += 400
        return clock[0]
    acct = account.run_claim(started["device_code"], "x", 5, 900, sleep=lambda s: None, now=now)
    assert acct is None and "expired" in account.claim_status()["error"]
results.append(run("an expired code ends the claim with a message, not a token", t5))


def t6():
    reset()
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    try:
        account.start_claim(background=False)
        raise AssertionError("a signed-in Mac must not start another claim")
    except RuntimeError as exc:
        assert "already signed in" in str(exc), exc
    account.forget()
    account.start_claim(background=False)
    try:
        account.start_claim(background=False)
        raise AssertionError("two claims at once must be refused")
    except RuntimeError as exc:
        assert "already waiting" in str(exc), exc
    account.cancel_claim()
    assert not account.claim_status()["running"]
    config.ACCOUNTS_URL = "off"
    try:
        account.start_claim(background=False)
        raise AssertionError("claims must be refused when accounts are off")
    except RuntimeError as exc:
        assert "turned off" in str(exc), exc
results.append(run("only one claim at a time, none when signed in or turned off", t6))


def t7():
    service = reset()
    assert account.whoami() == ("none", "not signed in")
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    assert account.whoami() == ("ok", "me@example.com"), account.whoami()
    service.token_ok = False
    state, detail = account.whoami()
    assert state == "revoked", (state, detail)
    assert account.load() is None, "a token the service rejects is forgotten"

    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))

    def down(*a, **k):
        raise ConnectionError("offline")
    account.transport = down
    state, detail = account.whoami()
    assert state == "unreachable" and "offline" in detail, (state, detail)
    assert account.load() is not None, "being offline is not being signed out"
results.append(run("whoami confirms, forgets a revoked token, and shrugs at being offline", t7))


def t8():
    service = reset()
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))
    account.sign_out()
    assert account.load() is None
    assert service.calls[-1][1].endswith("/device/revoke"), service.calls
    # Offline: signed out here regardless.
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))

    def down(*a, **k):
        raise ConnectionError("offline")
    account.transport = down
    account.sign_out()
    assert account.load() is None
results.append(run("signing out revokes the token when it can, and forgets it either way", t8))


def t9():
    name = account.device_name()
    assert isinstance(name, str) and name.strip(), repr(name)
results.append(run("this Mac has a name to sign in under", t9))


def t10():
    service = reset()
    assert account.register_public_url("https://panel.example.com") is False, "no account, nothing to register"
    account.save(account.Account("syd_abc", "a1", "me@example.com", "Me", "d1", "Mac",
                                 "syllabus", SERVICE, "x"))

    def answers(method, url, headers, body, timeout):
        service.calls.append((method, url, headers, body))
        path = url[len(SERVICE):]
        assert headers.get("Authorization") == "Bearer syd_abc", headers
        if path == "/device/public-url":
            return (200, {"ok": True}) if body["public_url"].startswith("https://") else (400, {"error": "invalid_request"})
        if path == "/panel/exchange":
            if body["code"] == "good":
                return 200, {"account": {"id": "a1", "email": "Me@Example.com", "name": "Me"}}
            return 400, {"error": "invalid_grant"}
        raise AssertionError(path)
    account.transport = answers
    assert account.register_public_url("https://panel.example.com") is True
    assert account.register_public_url("http://panel.example.com") is False, "the service's refusal is reported"
    assert account.exchange_code("good") == {"id": "a1", "email": "Me@Example.com", "name": "Me"}
    assert account.exchange_code("bad") is None

    def down(*a, **k):
        raise ConnectionError("offline")
    account.transport = down
    assert account.register_public_url("https://panel.example.com") is False
    try:
        account.exchange_code("good")
        raise AssertionError("network trouble during the exchange must raise, so the page can say so")
    except ConnectionError:
        pass
results.append(run("the panel registers its address and redeems a sign-in code with its token", t10))


print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
