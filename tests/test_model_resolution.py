"""Model aliases must survive retirements: the pinned id is used while
served; a retired id falls back to the lowest-versioned model still
available in its family; Models API trouble fails open to the pinned id."""

import pytest

import app.engine.anthropic_client as ac


class _Model:
    def __init__(self, id):
        self.id = id


class _FakeClient:
    def __init__(self, ids=None, error=False):
        self._ids, self._error = ids or [], error

    @property
    def models(self):
        outer = self

        class M:
            def list(self):
                if outer._error:
                    raise RuntimeError("api down")
                return [_Model(i) for i in outer._ids]

        return M()


@pytest.fixture(autouse=True)
def fresh_cache():
    ac._available_cache.update(at=0.0, ids=None)
    yield
    ac._available_cache.update(at=0.0, ids=None)


def _wire(monkeypatch, ids=None, error=False):
    monkeypatch.setattr(ac, "get_client", lambda: _FakeClient(ids, error))


def test_pinned_id_used_while_served(monkeypatch):
    _wire(monkeypatch, ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"])
    assert ac.resolve_model("opus") == "claude-opus-5"
    assert ac.resolve_model("sonnet") == "claude-sonnet-5"


def test_retired_alias_falls_back_to_lowest_available_in_family(monkeypatch):
    _wire(monkeypatch, ["claude-opus-6", "claude-opus-5-5", "claude-sonnet-6"])
    assert ac.resolve_model("opus") == "claude-opus-5-5"  # lowest still served
    assert ac.resolve_model("sonnet") == "claude-sonnet-6"
    # full ids get the same safety net
    assert ac.resolve_model("claude-opus-5") == "claude-opus-5-5"


def test_models_api_trouble_fails_open(monkeypatch):
    _wire(monkeypatch, error=True)
    assert ac.resolve_model("opus") == "claude-opus-5"
    # a stale cached list keeps serving through later failures
    ac._available_cache.update(at=0.0, ids={"claude-opus-5-5"})
    assert ac.resolve_model("opus") == "claude-opus-5-5"


def test_family_gone_returns_preferred_for_clear_api_error(monkeypatch):
    _wire(monkeypatch, ["claude-sonnet-6"])
    assert ac.resolve_model("opus") == "claude-opus-5"


def test_fallback_models_still_price_by_family():
    from app.engine.pricing import rates_for

    assert rates_for("claude-opus-5-5") == rates_for("claude-opus-5")
    assert rates_for("claude-sonnet-6") == (2.00, 10.00)
    assert rates_for("mystery") == rates_for("claude-opus-5")
