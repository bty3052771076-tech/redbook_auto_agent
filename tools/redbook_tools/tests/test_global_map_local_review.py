from __future__ import annotations

import hashlib
from types import SimpleNamespace

from PIL import Image, ImageDraw

from apps import cli
from src.storage.models import AssetInfo, Post
from src.workflow.review_cache import stamp_vision_cache


def _map_post(tmp_path):
    image_path = tmp_path / "world.png"
    image = Image.new("RGB", (1080, 1440), "#0b1324")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 180, 500, 500), fill=(42, 70, 90))
    image.save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    return Post(
        title="今日全球事件关注图｜2026-09-30",
        body="北京时间2026-09-30，三条可定位事件。",
        assets=[AssetInfo(path=str(image_path), kind="image", size_bytes=image_path.stat().st_size, sha256=digest, validated=True)],
        platform={
            "global_map": {
                "target_date": "2026-09-30",
                "cutoff": "2026-09-30T18:00:00+08:00",
                "source_state": "ready",
                "upload_allowed": True,
                "located_event_count": 3,
                "country_count": 3,
                "events": [
                    {
                        "event_key": key,
                        "title": title,
                        "country": country,
                        "latitude": latitude,
                        "longitude": longitude,
                        "location_precision": "city",
                        "location_method": "explicit_coordinates",
                        "evidence": [{
                            "title": title,
                            "summary": "",
                            "source": "Test Publisher",
                            "url": f"https://example.org/{key}",
                            "published_at": "2026-09-30T09:00:00+00:00",
                        }],
                    }
                    for key, title, country, latitude, longitude in (
                        ("china", "Factory opens in Beijing", "China", 39.9042, 116.4074),
                        ("germany", "Factory opens in Berlin", "Germany", 52.52, 13.405),
                        ("morocco", "Reconstruction completed in Rabat", "Morocco", 34.0209, -6.8416),
                    )
                ],
            },
            "render_report": {"map_sha256": digest, "feature_count": 175},
        },
    )


def test_locally_rendered_map_uses_artifact_review_instead_of_generic_vision(tmp_path):
    post = _map_post(tmp_path)
    review = getattr(cli, "_local_global_map_vision_result", lambda _post: None)(post)
    assert review is not None
    assert review["ok"] is True
    assert review["provider"] == "local_renderer"


def test_tampered_map_is_not_approved_as_local_render(tmp_path):
    post = _map_post(tmp_path)
    post.platform["render_report"]["map_sha256"] = "incorrect"
    review = getattr(cli, "_local_global_map_vision_result", lambda _post: None)(post)
    assert review is None


def test_source_evidence_required_even_when_asset_and_counters_match(tmp_path):
    post = _map_post(tmp_path)
    post.platform["global_map"]["events"][0]["evidence"] = []
    assert cli._local_global_map_vision_result(post) is None


def test_invalid_map_cannot_reuse_cached_vision(tmp_path, monkeypatch):
    post = _map_post(tmp_path)
    post.platform["global_map"]["events"][0]["evidence"] = []
    post.platform["quality_gate"] = {
        "vision": stamp_vision_cache(post, {"ok": True, "score": 100}),
    }
    monkeypatch.setenv("AUTO_VLM_REVIEW", "1")
    monkeypatch.setattr(cli, "list_posts", lambda: [])
    monkeypatch.setattr(cli, "validate_post_batch", lambda *args, **kwargs: SimpleNamespace(issues=[]))
    monkeypatch.setattr(cli, "save_post", lambda post: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("invalid source evidence must block before model review")

    monkeypatch.setattr(cli, "configured_vision_review_model", forbidden)
    monkeypatch.setattr(cli, "review_post_image", forbidden)
    errors = cli._run_auto_quality_gate(
        [post], expected_count=1, evaluation_viewpoint="", require_vision=True, reuse_vision_results=True,
    )
    assert any("MAP_EVIDENCE_UNVERIFIED" in error for error in errors)
    assert post.platform["quality_gate"]["deterministic_ok"] is False


def test_disabling_vision_does_not_erase_semantic_errors(tmp_path, monkeypatch):
    post = _map_post(tmp_path)
    post.platform["global_map"]["events"][0]["evidence"] = []
    monkeypatch.setenv("AUTO_VLM_REVIEW", "0")
    monkeypatch.setattr(cli, "list_posts", lambda: [])
    monkeypatch.setattr(cli, "validate_post_batch", lambda *args, **kwargs: SimpleNamespace(issues=[]))
    monkeypatch.setattr(cli, "save_post", lambda post: None)
    errors = cli._run_auto_quality_gate(
        [post], expected_count=1, evaluation_viewpoint="", require_vision=False,
    )
    assert any("MAP_EVIDENCE_UNVERIFIED" in error for error in errors)
