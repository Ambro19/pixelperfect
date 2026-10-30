# tests/suite/api/test_concurrency.py
# =============================================================================
# enforce_tier_concurrency: a per-user semaphore in front of the capture
# endpoint. Free 2, Pro 3, Business 5 simultaneous captures; the next one
# waits CONCURRENCY_ACQUIRE_TIMEOUT_SECONDS (0.3 s in tests) and then gets 429.
#
# These run the ASGI app in-process with httpx so several requests really are
# in flight at the same time on one event loop.
# =============================================================================
import asyncio

import httpx
import pytest

import main

URL = "https://example.com/"


async def _burst(user, n, path="/api/v1/screenshot"):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await asyncio.gather(*[
            ac.post(path, headers=user.headers, json={"url": URL}) for _ in range(n)
        ])


@pytest.mark.parametrize("tier, limit", [("free", 2), ("pro", 3), ("business", 5)])
async def test_concurrency_limit_per_tier(make_user, fake_shots, fake_storage, tier, limit):
    fake_shots.delay_s = 1.5          # far longer than the 0.3 s acquire timeout
    u = make_user(tier)
    responses = await _burst(u, limit + 2)
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * limit + [429, 429], codes
    assert fake_shots.max_in_flight == limit
    rejected = [r for r in responses if r.status_code == 429]
    assert all(r.headers.get("retry-after") == "1" for r in rejected)


async def test_limit_is_per_user_not_global(make_user, fake_shots, fake_storage):
    fake_shots.delay_s = 0.6
    a, b = make_user("free"), make_user("free")
    ra, rb = await asyncio.gather(_burst(a, 2), _burst(b, 2))
    assert [r.status_code for r in ra + rb] == [200] * 4


async def test_slot_is_released_after_failure(make_user, fake_shots, fake_storage):
    # A capture that errors must still give its slot back, or the user is
    # locked out after `limit` failures.
    u = make_user("free")
    fake_shots.raise_exc = KeyError("boom")
    for _ in range(4):
        [r] = await _burst(u, 1)
        assert r.status_code == 500
    fake_shots.raise_exc = None
    [r] = await _burst(u, 1)
    assert r.status_code == 200
    assert main._USER_LIMITERS[u.id].active == 0


async def test_quota_refusal_also_releases_slot(make_user, fake_shots, fake_storage,
                                                add_screenshots, monkeypatch):
    monkeypatch.setenv("FREE_SCREENSHOTS_LIMIT", "1")
    u = make_user("free")
    add_screenshots(u.id, 1)
    for _ in range(3):
        [r] = await _burst(u, 1)
        assert r.status_code == 429
    assert main._USER_LIMITERS[u.id].active == 0


async def test_upgrade_takes_effect_on_new_limiter(make_user, fake_shots, fake_storage, db):
    import models
    fake_shots.delay_s = 1.5
    u = make_user("free")
    await _burst(u, 1)                         # creates a free (2) limiter
    user = db.get(models.User, u.id)
    user.subscription_tier = "business"
    db.commit()
    responses = await _burst(u, 5)
    assert [r.status_code for r in responses] == [200] * 5
