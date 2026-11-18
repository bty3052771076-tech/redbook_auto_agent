"""Real LangChain acceptance prerequisite; only model responses are scripted."""

from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command, interrupt
import psycopg
from psycopg import sql
from pydantic import Field
import pytest

from src.knowledge.store import KnowledgeStore
from test_conversational_agent_cancellation import no_external_work


class ScriptedToolChatModel(BaseChatModel):
    responses: list[AIMessage]
    requests: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return 'offline-scripted-tool-model'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = len(self.requests)
        self.requests.append(deepcopy(messages))
        assert index < len(self.responses), 'Framework made an unexpected extra model request'
        return ChatResult(generations=[ChatGeneration(message=self.responses[index])])


@pytest.fixture
def pg_agent_checkpointer():
    """A second isolation boundary inside the root fixture's disposable DB."""
    knowledge = KnowledgeStore.from_env()
    schema = 'conversation_acceptance_' + uuid4().hex
    credentials = knowledge._credentials('migration')
    with psycopg.connect(**credentials, autocommit=True) as admin:
        database = admin.execute('SELECT current_database()').fetchone()[0]
        assert database.startswith('redbook_agent_test_'), 'Refusing a non-test PostgreSQL database'
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

    @contextmanager
    def open_saver():
        with psycopg.connect(**credentials, autocommit=True, prepare_threshold=0,
                             options='-c search_path=' + schema + ',public') as conn:
            saver = PostgresSaver(conn)
            saver.setup()
            assert conn.execute('SELECT current_schema()').fetchone()[0] == schema
            yield saver, conn

    try:
        yield schema, open_saver
    finally:
        with psycopg.connect(**credentials, autocommit=True) as admin:
            assert admin.execute('SELECT current_database()').fetchone()[0] == database
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_real_create_agent_tool_results_reach_next_model_request(pg_agent_checkpointer):
    # Import is deliberately not skipped: incompatible framework versions block release.
    from langchain.agents import create_agent

    _, open_saver = pg_agent_checkpointer
    writes = []

    @tool
    def read_source(source_id: str) -> str:
        """Read one local source fixture."""
        assert source_id == 'source-1'
        return 'verified-source-fixture'

    @tool
    def save_local_draft(body: str) -> str:
        """Save one local draft fixture."""
        writes.append(body)
        return 'fixture-draft-1'

    model = ScriptedToolChatModel(responses=[
        AIMessage(content='', tool_calls=[{'name': 'read_source', 'args': {'source_id': 'source-1'}, 'id': 'source-call'}]),
        AIMessage(content='', tool_calls=[{'name': 'save_local_draft', 'args': {'body': 'verified-source-fixture'}, 'id': 'save-call'}]),
        AIMessage(content='Saved fixture-draft-1.'),
    ])
    config = {'configurable': {'thread_id': 'framework-loop-' + uuid4().hex}}
    with open_saver() as (saver, conn):
        agent = create_agent(model=model, tools=[read_source, save_local_draft], checkpointer=saver)
        result = agent.invoke({'messages': [{'role': 'user', 'content': 'Read the source and save a local draft.'}]}, config)
        assert result['messages'][-1].content == 'Saved fixture-draft-1.'
        assert len(model.requests) == 3
        assert [(message.tool_call_id, message.content) for message in model.requests[1]
                if isinstance(message, ToolMessage)] == [('source-call', 'verified-source-fixture')]
        assert [(message.tool_call_id, message.content) for message in model.requests[2]
                if isinstance(message, ToolMessage)] == [
                    ('source-call', 'verified-source-fixture'), ('save-call', 'fixture-draft-1')]
        assert writes == ['verified-source-fixture']
        assert agent.get_state(config).next == ()
        assert conn.execute('SELECT count(*) FROM checkpoints WHERE thread_id=%s',
                            (config['configurable']['thread_id'],)).fetchone()[0] > 0


def test_dynamic_interrupt_survives_new_pg_connection_without_replaying_success(pg_agent_checkpointer):
    from langchain.agents import create_agent

    _, open_saver = pg_agent_checkpointer
    saved, published = [], []

    @tool
    def keep_completed_draft() -> str:
        """Keep a successful local draft before asking about a separate action."""
        saved.append('fixture-draft-1')
        return 'fixture-draft-1'

    @tool
    def gated_fixture_action(post_id: str) -> str:
        """Ask for explicit permission before a local side effect."""
        answer = interrupt({'kind': 'permission', 'tool': 'gated_fixture_action', 'post_id': post_id})
        if answer != {'allow': True, 'tool': 'gated_fixture_action', 'post_id': post_id}:
            return 'denied'
        published.append(post_id)
        return 'fixture-action-completed'

    tools = [keep_completed_draft, gated_fixture_action]
    config = {'configurable': {'thread_id': 'framework-interrupt-' + uuid4().hex}}
    first_model = ScriptedToolChatModel(responses=[
        AIMessage(content='', tool_calls=[{'name': 'keep_completed_draft', 'args': {}, 'id': 'keep-call'}]),
        AIMessage(content='', tool_calls=[{'name': 'gated_fixture_action', 'args': {'post_id': 'fixture-draft-1'}, 'id': 'gate-call'}]),
    ])
    with open_saver() as (saver, _):
        first_agent = create_agent(model=first_model, tools=tools, checkpointer=saver)
        pending = first_agent.invoke({'messages': [{'role': 'user', 'content': 'Keep the draft, then ask before the next action.'}]}, config)
        assert pending['__interrupt__'][0].value == {
            'kind': 'permission', 'tool': 'gated_fixture_action', 'post_id': 'fixture-draft-1'}
        assert len(first_model.requests) == 2
        assert saved == ['fixture-draft-1'] and published == []

    second_model = ScriptedToolChatModel(responses=[AIMessage(content='Completed only the missing action.')])
    with open_saver() as (saver, conn):
        resumed_agent = create_agent(model=second_model, tools=tools, checkpointer=saver)
        result = resumed_agent.invoke(Command(resume={
            'allow': True, 'tool': 'gated_fixture_action', 'post_id': 'fixture-draft-1'}), config)
        assert not result.get('__interrupt__')
        assert result['messages'][-1].content == 'Completed only the missing action.'
        assert saved == ['fixture-draft-1'], 'A completed draft was regenerated during resume'
        assert published == ['fixture-draft-1']
        assert len(second_model.requests) == 1
        assert resumed_agent.get_state(config).next == ()
        assert conn.execute('SELECT count(*) FROM checkpoints WHERE thread_id=%s',
                            (config['configurable']['thread_id'],)).fetchone()[0] > 0
