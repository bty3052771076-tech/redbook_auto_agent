"""Offline, fixed-input candidate replay. Never invokes a model or a platform."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

TOOLS=Path(__file__).resolve().parents[1]/"tools"/"redbook_tools"
sys.path.insert(0,str(TOOLS))


def replay(mode: str, *, count: int=10):
    from src.workflow import create_post as w
    from src.config import LLMConfig
    from src.news.daily_news import NewsItem
    facts=("市政府公布夜间公交调整计划，新增班次仍在研究，具体实施时间尚未确认。"
           "交通部门表示，换乘站将同步研究末班车衔接，运营公司需要根据客流调整车辆调度。"
           "乘客协会要求补充站点照明和候车安全措施，工会代表则提出司机轮班安排需要协商。"
           "财政部门解释，资金来源将使用既有交通专项预算，议会委员会将在审查报告中公开实施进展。")
    calls={"writer":0,"patch":0,"response_chars":0}
    results=[]
    def network_denied(*a,**kw):
        raise AssertionError("fixture benchmark cannot access external services")
    def scene(value,**kw):
        kw["audit"].update(accepted=value=="accepted",reason="fixture")
        return value if value=="accepted" else ""
    start=time.perf_counter()
    with patch.object(socket.socket,"connect",network_denied),patch.object(w,"save_post"),patch.object(w,"save_revision"),patch.object(w,"_normalize_daily_news_image_event",scene):
        for index in range(count):
            item=NewsItem(title="市政府公布夜间公交计划",url=f"https://fixture.invalid/{index}",description=facts,
                content=facts*3,seendate="2026-10-10 08:00:00",source="fixture",domain="fixture.invalid",language="zh")
            body=f"内容：\n{facts}\n\n评价：\n夜间班次仍待正式确认，乘客应以交通部门公布的时刻表为准。\n\n日期：{w._format_news_seendate(item.seendate)}"
            local_calls=[]
            def writer(*a,**kw):
                calls["writer"]+=1
                local_calls.append("writer")
                value={"title":item.title,"body":body,"topics":[],
                       "image_event":"bad" if index%3==0 and len(local_calls)==1 else "accepted"}
                calls["response_chars"]+=len(json.dumps(value,ensure_ascii=False))
                return value
            def repair(*a,**kw):
                calls["patch"]+=1
                value={"image_event":"accepted"}
                calls["response_chars"]+=len(json.dumps(value,ensure_ascii=False))
                return value
            with patch.object(w,"generate_draft",writer),patch.object(w,"generate_json",repair),w.ModelWorkQueues(llm_workers=1,image_workers=1) as queues:
                queues.pipeline_version=mode
                result=w._prepare_daily_news_candidate(candidate_index=index+1,picked=item,
                    cfgs=[LLMConfig(provider="fake",model="fixture",api_key="not-a-key")],asset_paths=[],
                    copy_assets=False,auto_image_enabled=False,prompt_norm="公交",viewpoint_norm="无视角评价",
                    target_count=1,single_material_mode=False,base_meta={},progress_callback=None,
                    model_queues=queues,post_quality_callback=None,prepared=(item,{}, {},item))
            results.append({"status":result.status,"reason":result.reason,
                            "content":w._daily_news_body_quality_fields(result.draft.get("body","")).get("内容","")})
    return {"mode":mode,"elapsed_seconds":time.perf_counter()-start,"calls":calls,"results":results,
            "measurement":"offline candidate review only; no real model latency, discovery, images or delivery"}


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument("--fixture-only",action="store_true",required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    root=Path("E:/AI/codex/redbook_runtime/data").resolve()
    output=args.output.resolve()
    if not output.is_relative_to(root):
        parser.error("output must be under the E-drive runtime/data directory")
    rows=[]
    for round_index in range(3):
        for mode in (("legacy","staged_v2") if round_index%2==0 else ("staged_v2","legacy")):
            rows.append({"round":round_index+1,**replay(mode)})
    output.mkdir(parents=True,exist_ok=True)
    (output/"report.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf8")
    for row in rows:
        print(json.dumps({k:v for k,v in row.items() if k not in {"results"}},ensure_ascii=True))
    if any(any(r["status"]!="success" for r in row["results"]) for row in rows):
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
