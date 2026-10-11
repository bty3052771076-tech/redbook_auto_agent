import json
import pytest
from src.ai_digest.models import AIUpdateItem
from src.ai_digest.incremental import summarize_ai_items


def items(count):
    return [AIUpdateItem(title=f"平台{i}开放模型预览",summary=f"平台{i}开放模型预览，开发者可通过官方接口调用并测试文本理解能力。",
        source_name=f"平台{i}",vendor=f"平台{i}",url=f"https://example{i}.org/release",published_at="2026-10-10",
        raw_excerpt=f"平台{i}开放模型预览，开发者可通过官方接口调用并测试文本理解能力。") for i in range(count)]


def response(rows):
    return {"items":[{"item_id":r["item_id"],"title":r["title"],"summary":r["summary"]} for r in rows]}


def test_missing_one_id_retries_only_that_id_and_keeps_all_items():
    calls=[]
    def invoke(rows):
        calls.append(rows)
        return response(rows[:-1] if len(calls)==1 else rows)
    source=items(10)
    result=summarize_ai_items(source,invoke=invoke,max_workers=1)
    assert len(result)==10
    assert [len(batch) for batch in calls]==[6,1,4]
    assert calls[1][0]["item_id"]==calls[0][-1]["item_id"]
    assert [i.url for i in result]==[i.url for i in source]
    assert [i.published_at for i in result]==[i.published_at for i in source]


def test_forged_dates_urls_and_extra_ids_are_not_adopted():
    source=items(2)
    def invoke(rows):
        out=response(rows)
        for item in out["items"]:
            item.update(url="https://forged.invalid",published_at="2099-01-01")
        out["items"].append({"item_id":"alien","title":"伪造","summary":"伪造"})
        return out
    result=summarize_ai_items(source,invoke=invoke)
    assert [i.url for i in result]==[i.url for i in source]
    assert [i.published_at for i in result]==[i.published_at for i in source]


def test_vague_summary_does_not_pass_or_loop():
    calls=[]
    def invoke(rows):
        calls.append(rows)
        return {"items":[{"item_id":r["item_id"],"title":"披露AI产品变化","summary":"披露相关变化"} for r in rows]}
    with pytest.raises(ValueError,match="AI_ITEM_SUMMARY_INVALID"):
        summarize_ai_items(items(1),invoke=invoke)
    assert len(calls)==2


def test_two_batches_overlap_without_exceeding_two_or_changing_order():
    from threading import Barrier, Lock
    active, peak = 0, 0
    barrier, lock = Barrier(2), Lock()
    def invoke(rows):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        result = response(rows)
        with lock:
            active -= 1
        return result
    source = items(12)
    result = summarize_ai_items(source, invoke=invoke)
    assert peak == 2
    assert [item.url for item in result] == [item.url for item in source]
