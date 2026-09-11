"""Vendor registry: seeding, classification, auto-registration, context."""

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.web.vendor_view as vv
from app.models import Base, Vendor


@pytest.fixture
def session_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def factory():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(vv, "db_session", factory)
    return factory


def test_seed_only_into_empty_table(session_factory):
    assert vv.seed_known_importers(session_factory) == len(vv.KNOWN_IMPORTERS)
    assert vv.seed_known_importers(session_factory) == 0  # never re-seeds
    with session_factory() as s:
        royal = s.scalar(select(Vendor).where(Vendor.name == "Royal Coffee"))
        assert royal.kind == "green_importer" and royal.confirmed
        # deleting one must not resurrect on "redeploy"
        s.delete(royal)
    assert vv.seed_known_importers(session_factory) == 0


def test_classify_email_by_domain_and_subdomain(session_factory):
    vv.seed_known_importers(session_factory)
    with session_factory() as s:
        assert vv.classify_email(s, "orders@royalcoffee.com").name == "Royal Coffee"
        assert vv.classify_email(s, "x@mail.royalcoffee.com").name == "Royal Coffee"
        assert vv.classify_email(s, "x@notroyalcoffee.com") is None
        assert vv.classify_email(s, "") is None


def test_register_vendor_impl_dedupes_and_lands_unconfirmed(session_factory):
    vv.seed_known_importers(session_factory)
    with session_factory() as s:
        made = vv.register_vendor_impl(s, "Pacific Bag", "pacificbag.com", "other",
                                       notes="invoice #88 for boxes")
        assert made["outcome"] == "created" and "unconfirmed" in made["note"]
        dupe = vv.register_vendor_impl(s, "Royal again", "royalcoffee.com", "other")
        assert dupe["outcome"] == "exists" and dupe["vendor"] == "Royal Coffee"
        bad = vv.register_vendor_impl(s, "", "nodomain", "other")
        assert bad["outcome"] == "error"
        s.commit()
        row = s.scalar(select(Vendor).where(Vendor.name == "Pacific Bag"))
        assert row.confirmed is False and row.notes == "invoice #88 for boxes"


def test_settings_management_roundtrip(session_factory):
    assert "needs a name" in vv.add_vendor("", "x.com", "other")
    assert "Added La Marzocco" in vv.add_vendor("La Marzocco", "lamarzoccousa.com", "other")
    assert "already belongs" in vv.add_vendor("Dupe", "lamarzoccousa.com", "other")
    groups = vv.vendor_groups()
    assert [v["name"] for v in groups["other"]] == ["La Marzocco"]
    vid = groups["other"][0]["id"]
    assert "Green bean importer" in vv.set_vendor_kind(vid, "green_importer")
    assert vv.vendor_groups()["green"][0]["name"] == "La Marzocco"
    assert "Removed La Marzocco" in vv.remove_vendor(vid)
    assert vv.vendor_groups() == {"green": [], "other": [], "unconfirmed": []}


def test_context_lists_vendors_for_agenda_only(session_factory):
    from app.engine.context import build_runtime_context
    from app.models import Routine

    with session_factory() as s:
        s.add(Vendor(name="Royal Coffee", kind="green_importer", domains="royalcoffee.com"))
        s.add(Vendor(name="Pacific Bag", kind="other", domains="pacificbag.com", confirmed=False))
        agenda = Routine(key="daily_agenda", name="A", system_prompt="p")
        tracker = Routine(key="lead_tracker", name="T", system_prompt="p")
        s.add_all([agenda, tracker])
        s.flush()
        agenda_ctx = build_runtime_context(s, agenda)
        tracker_ctx = build_runtime_context(s, tracker)
    assert "Known vendors" in agenda_ctx
    assert "GREEN COFFEE purchases" in agenda_ctx and "Royal Coffee (royalcoffee.com)" in agenda_ctx
    assert "awaiting Arda's confirmation" in agenda_ctx and "Pacific Bag" in agenda_ctx
    assert "register_vendor" in agenda_ctx
    assert "Known vendors" not in tracker_ctx
