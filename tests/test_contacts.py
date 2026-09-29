"""Contact directory for To/Cc/Bcc autocomplete: correspondents + leads,
bulk/muted/disregarded excluded, ranked by how often Boxx writes to them."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.web.contacts_view as cv
from app.models import Base, DisregardRule, Lead, LeadStage, MailMessage, MutedSender


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

    monkeypatch.setattr(cv, "db_session", factory)
    cv._cache.update(at=0.0, contacts=None)
    yield factory
    cv._cache.update(at=0.0, contacts=None)


def _mail(mailbox="arda", **kw):
    defaults = dict(mailbox=mailbox, sent_at=datetime.now(timezone.utc), is_outbound=False)
    defaults.update(kw)
    return MailMessage(**defaults)


def test_directory_ranks_correspondents_and_includes_leads(session_factory):
    now = datetime.now(timezone.utc)
    with session_factory() as s:
        # written to Eddie twice, Steph once
        for i in range(2):
            s.add(_mail(gmail_msg_id=f"o{i}", is_outbound=True,
                        to_addrs="Eddie N <eddie@fed.com>", sent_at=now - timedelta(days=i)))
        s.add(_mail(gmail_msg_id="o9", is_outbound=True, to_addrs="steph@213filming.com"))
        # Eddie's inbound reply carries his preferred display name
        s.add(_mail(gmail_msg_id="i1", from_addr="eddie@fed.com", from_name="Eddie Navarrette"))
        # inbound-only stranger: not a correspondent, not listed
        s.add(_mail(gmail_msg_id="i2", from_addr="stranger@rando.com", from_name="Rando"))
        # bulk / muted / disregarded never appear even when written to
        s.add(_mail(gmail_msg_id="o3", is_outbound=True,
                    to_addrs="noreply@spamco.com, muted@x.com, vetoed@y.com"))
        s.add(MutedSender(email="muted@x.com"))
        s.add(DisregardRule(contact_email="vetoed@y.com", title="t"))
        # a lead with no mail history still counts (explicit relationship)
        s.add(Lead(business_name="Pinecone Bakeshop", stage=LeadStage.CONTACTED,
                   contact_name="Austin Buccowich", contact_email="austin@pineconebakeshop.com"))

    contacts = cv.contact_directory()
    emails = [c["email"] for c in contacts]
    assert emails[0] == "eddie@fed.com"  # most written-to first
    assert "steph@213filming.com" in emails and "austin@pineconebakeshop.com" in emails
    for absent in ("stranger@rando.com", "noreply@spamco.com", "muted@x.com", "vetoed@y.com"):
        assert absent not in emails
    eddie = contacts[0]
    assert eddie["name"] == "Eddie Navarrette"  # inbound display name wins
    austin = next(c for c in contacts if c["email"] == "austin@pineconebakeshop.com")
    assert austin["name"] == "Austin Buccowich" and austin["hint"] == "Pinecone Bakeshop"


def test_directory_caches_and_fails_soft(session_factory, monkeypatch):
    with session_factory() as s:
        s.add(_mail(gmail_msg_id="o1", is_outbound=True, to_addrs="a@b.com"))
    first = cv.contact_directory()
    assert [c["email"] for c in first] == ["a@b.com"]

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(cv, "_build", boom)
    assert cv.contact_directory() == first  # served from cache
    cv._cache.update(at=0.0, contacts=None)
    assert cv.contact_directory() == []  # no cache + failure -> empty, never raises
