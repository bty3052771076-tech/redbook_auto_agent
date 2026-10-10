import json
import math
from pathlib import Path
from statistics import median
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_capability_store import store


ARTIFACTS=Path('E:/AI/codex/redbook_runtime/data/tmp/agent-controls-performance')


def report(name,samples,**metadata):
    ordered=sorted(samples)
    result={**metadata,'samples':len(samples),'p50_ms':median(samples),
            'p95_ms':ordered[math.ceil(.95*len(samples))-1]}
    ARTIFACTS.mkdir(parents=True,exist_ok=True)
    (ARTIFACTS/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(name+': '+json.dumps(result,ensure_ascii=False))
    return result


def test_warm_fifty_item_catalog_has_no_hidden_probe_and_records_latency(store,tmp_path,monkeypatch):
    from src.agent.capabilities.service import CapabilityService
    from src.agent.capabilities.models import digest
    from src.agent.mcp_manager import MCPManager
    schema={'type':'object','properties':{}}
    store.put('mcp_perf','mcp',{'name':'性能验收目录','transport':'stdio','enabled':False,
        'health':{'status':'unknown'},'tools':[{'name':'tool_'+str(i),'description':'只读参考',
            'input_schema':schema,'schema_hash':digest(schema)} for i in range(60)]},expected_revision=0)
    service=CapabilityService(tmp_path,store=store)
    monkeypatch.setattr(MCPManager,'list_tools',lambda *a,**k:pytest.fail('目录读取不应启动MCP'))
    monkeypatch.setattr(MCPManager,'call_tool',lambda *a,**k:pytest.fail('目录读取不应调用MCP'))
    first=service.catalog(limit=50)
    assert len(first['rows'])==50 and first['next_cursor']
    assert all('tools' not in row.get('connection',{}) for row in first['rows'])
    frozen=store.freeze(uuid4().hex,service.all_tools())
    assert len(frozen['tools']['mcp:mcp_perf:tool_0']['connection']['tools'])==60
    samples=[]
    for _ in range(20):
        started=perf_counter()
        result=service.catalog(limit=50)
        samples.append((perf_counter()-started)*1000)
        assert len(result['rows'])==50 and result['total']>=60
    measured=report('warm-catalog-50',samples,namespace=store.namespace,model_calls=0,mcp_calls=0)
    assert measured['p95_ms']<=500


def test_same_json_generation_separates_local_management_overhead(store,monkeypatch):
    from src.agent.capabilities.dispatcher import ToolDispatcher
    from src.agent.capabilities.registry import builtin_catalog
    from src.config import LLMConfig
    from src.llm import generate
    inputs=[]
    def invoke(messages):
        inputs.append([(message.type,message.content) for message in messages])
        return SimpleNamespace(content='{"title":"核验来源","body":"同一输入和模型"}')
    monkeypatch.setattr(generate,'init_chat_model',lambda *a,**k:SimpleNamespace(invoke=invoke))
    cfg=LLMConfig(model='offline-fixed-model',api_key='offline-not-real',base_url='http://127.0.0.1:19876')
    dispatcher=ToolDispatcher(store,store.freeze(uuid4().hex,builtin_catalog()),origin='diagnostic')
    def generate_one():
        return generate.generate_json(cfg,system_prompt='只输出事件JSON',user_prompt='同一条已核验事件材料')
    generate_one()
    unmanaged,managed=[],[]
    for _ in range(20):
        start=perf_counter()
        plain=generate_one()
        unmanaged.append((perf_counter()-start)*1000)
        start=perf_counter()
        controlled=dispatcher.call('builtin:news.generate',generate_one,stage='generate')
        managed.append((perf_counter()-start)*1000)
        assert plain==controlled=={'title':'核验来源','body':'同一输入和模型'}
    assert all(value==inputs[0] for value in inputs)
    a=report('same-input-unmanaged',unmanaged,model=cfg.model,network='offline-transport')
    b=report('same-input-managed',managed,model=cfg.model,network='offline-transport',namespace=store.namespace,
        local_p50_overhead_ms=median(managed)-median(unmanaged))
    assert a['samples']==b['samples']==20
    rows=store.calls(run_id=dispatcher.snapshot['run_id'])['rows']
    assert len(rows)==40 and all(row['status']=='succeeded' and row['origin']=='diagnostic' for row in rows)
