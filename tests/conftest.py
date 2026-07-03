"""
Shared test fixtures.

Session 1 introduced accounts: POST /api/tournaments and .../players now
require a signed-in user, and each account may hold only one player per
tournament. The existing mechanics tests (swiss, disputes, byes, scrubbing,
cross-tournament) create tournaments and add several players through the API
without any notion of auth.

Rather than rewrite every one of those setups, we override the `require_user_api`
dependency for the duration of each test so it mints a FRESH user on every call.
That does two things at once:
  * satisfies the auth gate (no 401s), and
  * gives each add_player call a distinct account, so "Alice" and "Bob" remain
    two different players under the one-player-per-account rule.

The real auth + lobby behavior is covered separately in test_accounts.py, which
drives the actual signup/login endpoints with real session cookies.
"""

import itertools
import pytest

_user_counter = itertools.count()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_auth: exercise the real signup/login flow instead of the "
        "fresh-user dependency override (used by test_accounts.py).",
    )


@pytest.fixture(autouse=True)
def fresh_user_per_request(request):
    # Auth-focused tests want the genuine dependency, not the override.
    if request.node.get_closest_marker("real_auth"):
        yield
        return

    from app.main import app
    from app.routers.auth import require_user_api
    from app.services import auth as auth_svc

    def _fresh_user():
        n = next(_user_counter)
        email = f"mech{n}@test.local"
        res = auth_svc.create_user(email, "password123", f"User{n}")
        # If this email somehow already exists in the current temp DB, fall back
        # to authenticating it — either way we return a real, FK-valid user id.
        if "user" in res:
            return res["user"]
        return auth_svc.authenticate(email, "password123")

    app.dependency_overrides[require_user_api] = _fresh_user
    yield
    app.dependency_overrides.pop(require_user_api, None)
