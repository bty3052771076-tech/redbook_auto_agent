import pytest

from src.ai_digest.models import AIUpdateItem
from src.ai_digest.rank import ai_update_history_key
from src.publish.draft_delivery import content_revision_fingerprint
from src.storage.files import save_post
from src.storage.models import Post, PostStatus
from src.workflow.create_post import _uploaded_ai_digest_history_keys


@pytest.fixture(autouse=True)
def isolated_history(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_DIGEST_HISTORY_SKIP_TODAY", "0")


def digest(post_id, *, event="mass", saved=True):
    item = AIUpdateItem(
        title=f"Sakana AI launches {event} research system",
        source_name="Sakana AI", source_type="official",
        url=f"https://sakana.ai/{event}/", published_at="2026-10-12",
        summary=f"Sakana AI launches {event} with a shared solver and evaluator.",
    )
    post = Post(id=post_id, title=f"Digest {post_id}", body=item.summary,
                uploaded=saved, platform={"ai_digest": {"items": [item.model_dump()]}})
    return post, ai_update_history_key(item)


def replacement_link(new, old):
    new.platform["agent_item_replacement"] = {
        "job_kind": "daily_ai_digest", "replaces_post_id": old.id,
        "replaces_version": content_revision_fingerprint(old),
    }


def confirm_replacement(new, destination):
    new.uploaded = True
    new.platform["agent_draft_replacement"] = {"xhs": {"post_id": destination.id}}
    new.platform["xhs_draft"] = {
        "revision_fingerprint": content_revision_fingerprint(new),
    }


def test_repair_excludes_only_the_draft_being_replaced():
    old, old_key = digest("old")
    independent, independent_key = digest("other", event="independent")
    save_post(old)
    save_post(independent)
    keys = _uploaded_ai_digest_history_keys(replacing_post=old)
    assert old_key not in keys
    assert independent_key in keys
    assert old_key in _uploaded_ai_digest_history_keys()


def test_same_event_in_an_independent_saved_draft_stays_blocked():
    old, key = digest("old")
    other, _ = digest("other")
    save_post(old)
    save_post(other)
    assert key in _uploaded_ai_digest_history_keys(replacing_post=old)


def test_repeated_repair_follows_only_version_matched_ancestors():
    old, key = digest("old")
    leaf, _ = digest("leaf", event="leaf", saved=False)
    replacement_link(leaf, old)
    save_post(old)
    save_post(leaf)
    assert key not in _uploaded_ai_digest_history_keys(replacing_post=leaf)
    old.body = "Changed independently after the replacement was generated"
    save_post(old)
    assert key in _uploaded_ai_digest_history_keys(replacing_post=leaf)


@pytest.mark.parametrize("publication", ["status", "receipt"])
def test_already_published_content_cannot_be_excluded(publication):
    old, key = digest("published")
    if publication == "status":
        old.status = PostStatus.published
    else:
        old.platform["xhs_publication"] = {"visibility": "public"}
    leaf, _ = digest("leaf", event="leaf", saved=False)
    replacement_link(leaf, old)
    save_post(old)
    save_post(leaf)
    assert key in _uploaded_ai_digest_history_keys(replacing_post=old)
    assert key in _uploaded_ai_digest_history_keys(replacing_post=leaf)


def test_confirmed_replacement_no_longer_poisoned_by_superseded_history():
    old, old_key = digest("old")
    new, new_key = digest("new", event="new", saved=False)
    replacement_link(new, old)
    confirm_replacement(new, old)
    save_post(old)
    save_post(new)
    keys = _uploaded_ai_digest_history_keys()
    assert old_key not in keys
    assert new_key in keys


@pytest.mark.parametrize("state", ["pending", "stale_receipt", "wrong_destination", "wrong_column"])
def test_unconfirmed_or_unrelated_replacement_keeps_old_platform_history(state):
    old, key = digest("old")
    new, _ = digest("new", event="new", saved=False)
    replacement_link(new, old)
    if state != "pending":
        confirm_replacement(new, old)
    if state == "stale_receipt":
        new.body = "Edited after the last platform save"
    elif state == "wrong_destination":
        new.platform["agent_draft_replacement"]["xhs"]["post_id"] = "unrelated"
    elif state == "wrong_column":
        new.platform["agent_item_replacement"]["job_kind"] = "daily_wool"
    save_post(old)
    save_post(new)
    assert key in _uploaded_ai_digest_history_keys()


def test_confirmed_second_repair_replaces_the_original_destination():
    old, old_key = digest("old")
    middle, _ = digest("middle", event="middle", saved=False)
    new, new_key = digest("new", event="new", saved=False)
    replacement_link(middle, old)
    replacement_link(new, middle)
    confirm_replacement(new, old)
    for post in (old, middle, new):
        save_post(post)
    keys = _uploaded_ai_digest_history_keys()
    assert old_key not in keys
    assert new_key in keys
