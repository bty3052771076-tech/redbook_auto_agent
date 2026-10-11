from dataclasses import replace
from src.workflow.news_contracts import StageContext


def test_resume_reuses_but_policy_source_and_account_changes_invalidate():
    c=StageContext("root","attempt-1","news","item","account","frozen",pipeline_version="staged_v2")
    key=c.cache_key("writer",{"source":"original"})
    assert replace(c,execution_id="attempt-2").cache_key("writer",{"source":"original"})==key
    assert c.cache_key("writer",{"source":"updated"})!=key
    for field in ("namespace","config_hash","rule_version","capability_hash"):
        assert replace(c,**{field:"changed"}).cache_key("writer",{"source":"original"})!=key


def test_news_and_ai_resume_keep_root_not_execution_identity():
    from src.workflow.news_execution import job_stage_options
    from types import SimpleNamespace
    context = {"agent_run_id": "original", "generation_pipeline_version": "staged_v2"}
    for kind in ("daily_news", "daily_ai_digest"):
        job = SimpleNamespace(kind=kind, count=1)
        first = job_stage_options(job, context, run_id="first", performance_mode="speed")["stage_context"]
        resumed = job_stage_options(job, context, run_id="resumed", performance_mode="speed")["stage_context"]
        assert first.root_run_id == resumed.root_run_id == "original"
        assert first.job_key == resumed.job_key == kind
        assert first.execution_id != resumed.execution_id
        assert first.config_hash == resumed.config_hash


def test_old_json_checkpoint_keeps_legacy_even_when_new_default_selected(tmp_path):
    import json
    from types import SimpleNamespace
    from src.agent.editorial_agent import AgentJob, EditorialAgentTools, EditorialAgentConfig, run_editorial_agent
    pointer=tmp_path/"old"/"checkpoint.json"
    pointer.parent.mkdir()
    pointer.write_text(json.dumps({"run_id":"old", "jobs":[{"kind":"daily_news","title":"News","count":1}]}),encoding="utf8")
    contexts=[]
    def generate(job,context):
        contexts.append(context["generation_pipeline_version"])
        return [SimpleNamespace(id="one")]
    run_editorial_agent([AgentJob("daily_news","News")],
        tools=EditorialAgentTools(lambda j:{},generate,lambda *a:[],lambda *a:(True,"saved")),
        config=EditorialAgentConfig(checkpoint_dir=tmp_path,resume_from=pointer,generation_pipeline_version="staged_v2"))
    assert contexts==["legacy"]
