"""Authenticated conversation controls, separate from slow model/tool work."""
from typing import Literal
from uuid import uuid4
from threading import Lock

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .dialogue import DialogueService


class MessageInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=12000)
    client_message_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r'^[A-Za-z0-9_-]{16,80}$')


class ControlInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['pause', 'cancel']
    execution_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve_once', 'approve_session', 'deny', 'answer', 'configured']
    version: int = Field(ge=1, strict=True)
    answer: str = Field(default='', max_length=4000)
    max_calls: int = Field(default=1, ge=1, le=20, strict=True)


class GoalInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['set', 'edit', 'pause', 'resume', 'clear', 'complete']
    version: int = Field(ge=0, strict=True)
    objective: str = Field(default='', max_length=4000)
    stage: Literal['local', 'reviewed', 'saved', 'manual'] = 'local'
    target: int = Field(default=1, ge=1, le=100, strict=True)
    max_turns: int = Field(default=8, ge=1, le=50, strict=True)


def create_dialogue_router(get_current, app):
    router = APIRouter()
    initialize = Lock()

    def host():
        current = get_current()
        existing = getattr(app.state, 'dialogue', None)
        if existing is None or existing.current is not current:
            with initialize:
                existing = getattr(app.state, 'dialogue', None)
                if existing is None or existing.current is not current:
                    existing = DialogueService(current)
                    app.state.dialogue = existing
        return existing

    def call(function):
        try:
            return function()
        except KeyError:
            raise HTTPException(404, '对话或请求不存在') from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get('/api/conversations/{cid}/assistant')
    def state(cid: str):
        return call(lambda: host().snapshot(cid))

    @router.post('/api/conversations/{cid}/assistant/messages', status_code=202)
    def message(cid: str, body: MessageInput):
        return call(lambda: host().message(cid, body.content, body.client_message_id))

    @router.post('/api/conversations/{cid}/assistant/control')
    def control(cid: str, body: ControlInput):
        return call(lambda: host().control(cid, body.action, body.execution_id))

    @router.post('/api/conversations/{cid}/assistant/goal')
    def goal(cid: str, body: GoalInput):
        return call(lambda: host().goal(cid, body.model_dump()))

    @router.post('/api/conversations/{cid}/assistant/requests/{identity}', status_code=202)
    def answer(cid: str, identity: str, body: AnswerInput):
        return call(lambda: host().answer(cid, identity, body.model_dump()))

    return router
