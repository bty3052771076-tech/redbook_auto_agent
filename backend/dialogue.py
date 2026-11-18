"""Non-blocking conversation host; the child runs the real framework agent."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
from threading import Lock, Thread
import time
from uuid import uuid4

from src.agent.capabilities.models import safe
from src.integrations.local_process import terminate_task_tree, release_process_services
from src.workflow.resources import process_alive, process_identity

from .dialogue_store import ACTIVE, DialogueStore


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'data/dialogue'


class DialogueService:
    def __init__(self, current, *, store=None, directory=DIRECTORY):
        self.current = current
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store = store or DialogueStore(current.conversation_store.knowledge_store,
                                             namespace=current.conversation_store.namespace)
        self.store.setup()
        self.processes = {}
        self.guards = {}
        self.lock = Lock()

    def guard(self, cid):
        with self.lock:
            return self.guards.setdefault(cid, Lock())

    def snapshot(self, cid):
        state = self.store.session(cid)
        with self.lock:
            owned = self.processes.get(cid)
        if (state['state'] in ACTIVE and not owned and state['pid']
                and not process_alive(state['pid'], state['process_identity'])):
            self.store.abandon(cid, state['execution_id'])
            final = 'cancelled' if state['state'] == 'stopping' and state['input'].get('control') == 'cancel' else 'paused'
            self.store.transition(cid, expected_execution_id=state['execution_id'], state=final, pid=None, process_identity=None)
            self.store.event(cid, 'host/recovered', {'text': '执行进程已退出，已保留检查点和产物；未知动作不会自动重放。'},
                             key='recovered:' + state['execution_id'])
        return self.store.snapshot(cid)

    def environment(self, frozen=None):
        from src.model_platforms.integration import freeze_run_environment
        env = self.current.environment()
        if frozen:
            env.update(frozen)
        else:
            env = self.current.freeze_model_roles(env, {'kind': 'agent'})
            image = json.loads(env.get('RUN_LEGACY_MODEL_ROLES') or '{}').get('image')
            if image:
                provider, _, model = image.partition(':')
                if provider not in {'minimax', 'ccswitch', 'opencodex', 'aliyun', 'volcengine', 'siliconflow'} or not model:
                    raise ValueError('IMAGE_MODEL_NOT_CONFIGURED: 请在连接与模型选择已适配的生图模型')
                env.update(IMAGE_PROVIDER=provider, AUTO_IMAGE='1')
                env[provider.upper() + '_IMAGE_MODEL'] = model
        env.update(REDBOOK_RUNTIME_ROOT=str(self.current.root),
                   KNOWLEDGE_DB_CREDENTIALS=str(self.store.knowledge.credentials_path),
                   MODEL_PLATFORMS_NAMESPACE='agent',
                   MODEL_PLATFORMS_DIR=str(self.current.root / 'data/model_platforms'),
                   AUTO_VLM_SCORE_REQUIRED='0', ALLOW_PAID_LLM_FALLBACK='0',
                   MINIMAX_ALLOW_PAYGO='0', MINIMAX_ALLOW_PAID_CREDITS='0')
        env.update(freeze_run_environment(env, roles=('agent', 'writer')))
        return env

    def message(self, cid, text, client_id):
        text = str(text).strip()
        if not text:
            raise ValueError('消息不能为空')
        controls = {'暂停': 'pause', '暂停当前目标': 'pause', '暂停当前任务': 'pause',
                    '停止': 'pause', '取消': 'cancel', '取消当前目标': 'cancel', '取消当前任务': 'cancel'}
        if text in controls:
            self.store.message(cid, 'user', text, identity=client_id)
            return self.control(cid, controls[text])
        old_event = self.store.query('''SELECT payload FROM agent.dialogue_events
            WHERE namespace=%s AND conversation_id=%s AND event_key=%s''',
            (self.store.namespace, cid, 'input:' + client_id), one=True)
        if old_event:
            if old_event['payload'].get('text') != text:
                raise ValueError('MESSAGE_ID_CONFLICT')
            failed = self.store.session(cid)
            if (failed['input'].get('launch_failed') and failed['input'].get('message_id') == client_id
                    and failed['state'] not in ACTIVE):
                return self._start(cid, {'kind': 'message', 'text': text, 'message_id': client_id})
            return self.snapshot(cid)
        state = self.store.session(cid)
        if state['state'] == 'cancelled' and text in {'继续', '恢复', '接着'}:
            raise ValueError('DIALOGUE_CANCELLED: 已取消的执行不会恢复，请明确新的目标')
        self.store.message(cid, 'user', text, identity=client_id)
        self.store.event(cid, 'user/message', {'text': text, 'message_id': client_id}, key='input:' + client_id)
        if state['state'] in ACTIVE:
            if state['state'] != 'stopping':
                self.control(cid, 'pause', state['execution_id'], steering=True)
            with self.guard(cid):
                state = self.store.session(cid)
                queued = list(state['input'].get('pending_messages', []))
                queued.append({'id': client_id, 'text': text})
                self.store.transition(cid, expected_execution_id=state['execution_id'],
                    input={**state['input'], 'pending_messages': queued}, permissions={})
            self.store.event(cid, 'user/queued', {'text': '补充要求已保存；确认旧轮次停止后送入同一对话。'})
            Thread(target=self._continue_queued, args=(cid,), name='dialogue-handoff', daemon=True).start()
            return self.snapshot(cid)
        self.store.query('''UPDATE agent.dialogue_requests SET status='superseded',updated_at=%s
            WHERE namespace=%s AND conversation_id=%s AND status='pending' ''',
            (time.time(), self.store.namespace, cid))
        return self._start(cid, {'kind': 'message', 'text': text, 'message_id': client_id})

    def goal(self, cid, request):
        action = request["action"]
        with self.guard(cid):
            session = self.store.session(cid)
            old = session["goal"]
            version = old.get("version", 0)
            if version != request["version"]:
                raise ValueError("GOAL_CHANGED: 目标已更新，请刷新")
            stage = old.get("state")
            ready = session["state"] in {"idle", "paused", "cancelled"}
            if ready and action in {"set", "edit", "resume"}:
                ready = not self.store.query('''SELECT id FROM agent.dialogue_requests
                    WHERE namespace=%s AND conversation_id=%s AND execution_id=%s AND status='pending' LIMIT 1''',
                    (self.store.namespace, cid, session["execution_id"]), one=True)
            if action == "set":
                if not ready or stage in {"active", "paused", "blocked"}:
                    raise ValueError("GOAL_BUSY: 请结束本轮，并先清除已有目标")
                updated = {"id": uuid4().hex, "started_at": time.time(), "turns": 0, "state": "active"}
            else:
                if not old or stage in {"cleared", "completed"} and action != "clear":
                    raise ValueError("GOAL_NOT_ACTIVE: 目标已结束")
                if action == "complete" and (old["stage"] != "manual" or not ready):
                    raise ValueError("GOAL_VERIFY_REQUIRED: 请等待执行结束，由可核验交付条件验收")
                if action == "edit" and not ready:
                    raise ValueError("GOAL_BUSY: 请处理待确认请求或暂停执行后再编辑")
                updated = {**old}
                updated["state"] = {
                    "pause": "paused", "resume": "active", "clear": "cleared", "complete": "completed",
                }.get(action, stage)
            if action in {"set", "edit"}:
                objective = request["objective"].strip()
                if not objective:
                    raise ValueError("GOAL_EMPTY: 请写下目标")
                updated.update(objective=objective, stage=request["stage"], target=request["target"],
                               max_turns=request["max_turns"])
            if action == "resume" and updated["turns"] >= updated["max_turns"]:
                raise ValueError("GOAL_LIMIT_REACHED: 调高轮次上限后再恢复")
            if action == "complete":
                updated["ended_at"] = time.time()
            updated.update(version=version + 1, reason="人工验收已确认。" if action == "complete" else "")
            if not self.store.transition(cid, expected_goal=old, expected_state=session["state"], goal=updated):
                raise ValueError("GOAL_CHANGED: 执行或目标已更新，请刷新")
            self.store.event(cid, "goal/" + action, {"text": (
                updated["objective"] if action in {"set", "edit"} else updated["reason"]
            )})
        if ready and action in {"set", "resume"}:
            return self._start(cid, {"kind": "goal"}, goal_version=updated["version"])
        return self.snapshot(cid)

    def _continue_queued(self, cid):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.store.session(cid)
            if state['state'] not in ACTIVE:
                if state['state'] in {'paused', 'cancelled'} and state['input'].get('pending_messages'):
                    try:
                        self._start(cid, {'kind': 'message', 'messages': state['input']['pending_messages']})
                    except ValueError:
                        if self.store.session(cid)['state'] not in ACTIVE:
                            raise
                return
            time.sleep(.1)
        self.store.event(cid, 'host/error', {'text': '旧执行尚未确认停止，补充消息已保留，未并发开始新操作。'})

    def _start(self, cid, inputs, *, goal_version=None):
        with self.guard(cid):
            state = self.store.session(cid)
            if state['state'] in ACTIVE:
                raise ValueError('DIALOGUE_BUSY: 此对话已有执行轮次')
            if goal_version is not None and (
                state['goal'].get('version') != goal_version or state['goal'].get('state') != 'active'
                or state['state'] not in {'idle', 'paused', 'cancelled'}
            ):
                raise ValueError('GOAL_CHANGED: 目标状态已更新，未启动新轮次')
            execution = uuid4().hex
            permissions = {}
            inputs = {**inputs, 'round_started_at': (state['input'].get('round_started_at') or time.time())
                      if inputs['kind'] == 'resume' else time.time()}
            if inputs['kind'] == 'resume':
                permissions = {k: {**v, 'execution_id': execution} for k, v in state['permissions'].items()
                               if v.get('execution_id') == state['execution_id']}
            updated = self.store.transition(cid, expected_execution_id=state['execution_id'], state='starting',
                execution_id=execution, input=inputs, pid=None, process_identity=None,
                permissions=permissions, model_env=state['model_env'] if inputs['kind'] == 'resume' else {})
            if updated is None:
                raise ValueError('DIALOGUE_CONFLICT: 执行状态已更新，请刷新')
            if inputs['kind'] == 'resume':
                self.store.query('''UPDATE agent.dialogue_requests SET execution_id=%s
                    WHERE namespace=%s AND conversation_id=%s AND execution_id=%s
                    AND (status='pending' OR id=%s)''',
                    (execution, self.store.namespace, cid, state['execution_id'], inputs['request_id']))
            thread = Thread(target=self._drive, args=(cid, execution), name='dialogue-' + execution[:8], daemon=True)
            try:
                thread.start()
            except Exception:
                self.store.transition(cid, expected_execution_id=execution, state='paused',
                    execution_id=state['execution_id'], input={**inputs, 'launch_failed': True},
                    permissions=state['permissions'], model_env=state['model_env'])
                self.store.query('''UPDATE agent.dialogue_requests SET execution_id=%s
                    WHERE namespace=%s AND conversation_id=%s AND execution_id=%s''',
                    (state['execution_id'], self.store.namespace, cid, execution))
                raise
        return self.snapshot(cid)

    def _drive(self, cid, execution):
        process, identity = None, None
        folder = self.directory / cid
        try:
            folder.mkdir(parents=True, exist_ok=True)
            session = self.store.session(cid)
            env = self.environment(session.get('model_env'))
            public_keys = {'RUN_MODEL_SNAPSHOTS', 'RUN_LEGACY_MODEL_ROLES', 'RUN_LEGACY_MODEL_CONFIGS',
                           'MODEL_PLATFORMS_DIR', 'MODEL_PLATFORMS_NAMESPACE', 'CONTROLLER_MODEL_REF',
                           'WRITER_MODEL_REF', 'IMAGE_MODEL_REF', 'IMAGE_PROVIDER', 'AUTO_IMAGE',
                           'AGENT_LLM_PROVIDER', 'LLM_PROVIDER', 'AUTO_VLM_SCORE_REQUIRED'}
            frozen = {k: v for k, v in env.items() if k in public_keys or k.endswith('_IMAGE_MODEL')}
            overlay = ROOT / 'data/assistant-python'
            if not (overlay / 'langgraph/prebuilt').is_dir():
                raise RuntimeError('DIALOGUE_FRAMEWORK_UNAVAILABLE: 缺少已验证的 E 盘私有框架依赖，未降级到旧批处理')
            env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1',
                       PYTHONPATH=os.pathsep.join([str(overlay), str(ROOT), str(ROOT / 'tools/redbook_tools')]),
                       TEMP=str(ROOT / 'data/tmp'), TMP=str(ROOT / 'data/tmp'),
                       DIALOGUE_DIRECTORY=str(self.directory), DIALOGUE_OWNER_PID=str(os.getpid()),
                       DIALOGUE_OWNER_IDENTITY=str(process_identity(os.getpid())))
            with self.guard(cid):
                state = self.store.session(cid)
                if state['execution_id'] != execution or state['state'] != 'starting':
                    return
                self.store.transition(cid, expected_execution_id=execution, model_env=frozen)
                process = subprocess.Popen([sys.executable, '-u', '-m', 'backend.dialogue_worker',
                    cid, execution, self.store.namespace], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                identity = process_identity(process.pid)
                with self.lock:
                    self.processes[cid] = (execution, process, identity)
                self.store.transition(cid, expected_execution_id=execution, pid=process.pid, process_identity=identity)
            with (folder / (execution + '.log')).open('a', encoding='utf-8') as log:
                last_event = 0.0
                for line in process.stdout:
                    text = self.current.redact(line.rstrip())[:4000]
                    log.write(text + '\n')
                    log.flush()
                    if time.monotonic() - last_event >= 1:
                        self.store.event(cid, 'worker/progress', {'text': text, 'execution_id': execution})
                        last_event = time.monotonic()
            code = process.wait()
            state = self.store.session(cid)
            if state['execution_id'] == execution and state['state'] in {'starting', 'running'}:
                self.store.abandon(cid, execution)
                self.store.transition(cid, expected_execution_id=execution, state='waiting_input')
                self.store.request(cid, execution, 'error', {
                    'tool': 'controller', 'reason': f'执行进程退出（{code}），未确认整个目标完成。检查点及未知动作已保留。',
                    'allowed_decisions': ['answer'], 'arguments': {}}, key='exit:' + execution)
        except Exception as exc:
            text = self.current.redact(str(exc))[:2000]
            state = self.store.session(cid)
            if state['execution_id'] == execution and state['state'] != 'stopping':
                self.store.transition(cid, expected_execution_id=execution, state='waiting_input')
                self.store.event(cid, 'host/error', {'text': text})
                self.store.request(cid, execution, 'configuration', {'tool': 'controller', 'reason': text,
                    'arguments': {}, 'allowed_decisions': ['configured', 'answer']}, key='setup:' + execution)
        finally:
            if process is not None and process.poll() is not None:
                release_process_services(process.pid, identity)
            with self.lock:
                item = self.processes.get(cid)
                if item and item[0] == execution:
                    self.processes.pop(cid, None)

    def control(self, cid, action, execution_id=None, *, steering=False):
        if action not in {'pause', 'cancel'}:
            raise ValueError('CONTROL_INVALID')
        state = self.store.session(cid)
        if execution_id and state['execution_id'] != execution_id:
            raise ValueError('EXECUTION_CHANGED: 请选择当前执行，不会停止其他轮次')
        if state['state'] in {'paused', 'cancelled'} and (action == 'pause' or state['state'] == 'cancelled'):
            return self.snapshot(cid)
        execution = state['execution_id']
        with self.guard(cid):
            state = self.store.session(cid)
            if state['execution_id'] != execution:
                raise ValueError('EXECUTION_CHANGED')
            already_stopping = state['state'] == 'stopping'
            if already_stopping and state['input'].get('control') == 'cancel':
                action = 'cancel'
            values = {'state': 'stopping', 'input': {**state['input'], 'control': action},
                      'control_epoch': state['control_epoch'] + 1}
            goal = state['goal']
            if not steering and goal.get('state') == 'active':
                values['goal'] = {**goal, 'state': 'paused', 'version': goal['version'] + 1,
                                  'reason': '当前执行已停止推进，已有产物和目标均保留。'}
            if action == 'cancel':
                values['permissions'] = {}
                values['input'].pop('pending_messages', None)
            if not self.store.transition(cid, expected_execution_id=execution,
                                         expected_goal=goal if 'goal' in values else None, **values):
                raise ValueError('EXECUTION_CHANGED')
            if action == 'cancel':
                self.store.query('''UPDATE agent.dialogue_requests SET status='cancelled',updated_at=%s
                    WHERE namespace=%s AND conversation_id=%s AND execution_id=%s AND status='pending' ''',
                    (time.time(), self.store.namespace, cid, execution))
            self.store.event(cid, 'host/stopping', {'action': action, 'text': '正在停止对应执行；已保留产物和未知动作。'})
            self.store.event(cid, 'user/control', {'action': action,
                'text': '用户已取消旧执行，不恢复旧计划。' if action == 'cancel' else '用户暂停了旧轮次，未开始动作不得执行。'})
        if not already_stopping:
            Thread(target=self._stop, args=(cid, execution, action), name='dialogue-stop', daemon=True).start()
        return self.snapshot(cid)

    def _stop(self, cid, execution, action):
        try:
            with self.guard(cid):
                state = self.store.session(cid)
                if state['execution_id'] != execution:
                    return
                with self.lock:
                    item = self.processes.get(cid)
                pid = item[1].pid if item and item[0] == execution else state['pid']
                identity = item[2] if item and item[0] == execution else state['process_identity']
            if pid and process_alive(pid, identity):
                terminate_task_tree(pid, identity)
                deadline = time.monotonic() + 3
                while process_alive(pid, identity) and time.monotonic() < deadline:
                    time.sleep(.05)
                if process_alive(pid, identity):
                    raise RuntimeError('STOP_UNCONFIRMED: 未确认进程退出，请保留检查点，不要重复执行')
            with self.guard(cid):
                state = self.store.session(cid)
                if state['execution_id'] != execution or state['state'] != 'stopping':
                    return
                self.store.abandon(cid, execution)
                final = 'cancelled' if state['input'].get('control', action) == 'cancel' else 'paused'
                self.store.transition(cid, expected_execution_id=execution, state=final, pid=None, process_identity=None)
                self.store.event(cid, 'host/' + final, {'text': '执行已停止，已有稿件及草稿回执保留。'})
        except Exception as exc:
            self.store.event(cid, 'host/error', {'text': self.current.redact(str(exc))[:2000]})

    def answer(self, cid, request_id, body):
        state = self.store.session(cid)
        if state['state'] in ACTIVE or state['state'] == 'cancelled':
            raise ValueError('DIALOGUE_BUSY_OR_CANCELLED: 请核对当前状态，未重复执行')
        row = self.store.query('''SELECT * FROM agent.dialogue_requests
            WHERE namespace=%s AND conversation_id=%s AND id=%s''', (self.store.namespace, cid, request_id), one=True)
        if not row:
            raise ValueError('REQUEST_NOT_FOUND')
        answer = {k: v for k, v in body.items() if k != 'version'}
        if answer['decision'] not in row['payload'].get('allowed_decisions', []):
            raise ValueError('REQUEST_DECISION_DENIED')
        if row['status'] == 'answered':
            if row['answer'] == answer:
                if state['input'].get('request_id') == request_id and not state['input'].get('launch_failed'):
                    return self.snapshot(cid)
                if row['execution_id'] == state['execution_id']:
                    return self._start(cid, {'kind': 'resume', 'request_id': request_id, 'answer': answer})
                raise ValueError('REQUEST_EXECUTION_CHANGED: 该请求不属于当前执行')
            raise ValueError('REQUEST_ALREADY_ANSWERED')
        if row['execution_id'] != state['execution_id']:
            raise ValueError('REQUEST_EXECUTION_CHANGED: 该请求不属于当前执行')
        self.store.answer(cid, request_id, body['version'], answer)
        self.store.event(cid, 'user/decision', {'request_id': request_id, **answer})
        return self._start(cid, {'kind': 'resume', 'request_id': request_id, 'answer': answer})

    def close(self):
        with self.lock:
            rows = [(cid, item[0]) for cid, item in self.processes.items()]
        for cid, execution in rows:
            self.control(cid, 'pause', execution)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not any(self.store.session(cid)['state'] in ACTIVE for cid, _ in rows):
                return
            time.sleep(.05)
