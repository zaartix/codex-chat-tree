"""Persisted Codex history and deletion; never a source of live Desktop status."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class CodexHistory:
    def __init__(self, executable=None):
        bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex')
        self.executable = executable or os.environ.get('CODEX_CLI_PATH') or (str(bundled) if bundled.exists() else shutil.which('codex'))

    @staticmethod
    def _next_event(messages, deadline, phase):
        try:
            event=messages.get(timeout=max(.01,deadline-time.monotonic()))
        except queue.Empty as error:
            raise RuntimeError('Codex AppServer timed out during '+phase+'; inspect the recorded operation before retrying') from error
        if 'id' in event and 'method' in event:
            raise RuntimeError('Codex AppServer requires interactive confirmation: '+event['method']+'; inspect the saved chat in Desktop')
        return event

    @contextmanager
    def connection(self):
        if not self.executable:
            raise RuntimeError('Codex CLI not found')
        process = subprocess.Popen([self.executable, 'app-server', '--stdio'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        messages = queue.Queue()

        def receive():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        reader=threading.Thread(target=receive, daemon=True)
        reader.start()

        def send(value):
            process.stdin.write(json.dumps(value) + '\n')
            process.stdin.flush()

        def call(identifier, name, arguments):
            send({'jsonrpc': '2.0', 'id': identifier, 'method': name, 'params': arguments})
            deadline = time.monotonic() + 20
            deferred = []
            while True:
                result = self._next_event(messages,deadline,name)
                if result.get('method') in ('turn/completed', 'thread/settings/updated'):
                    deferred.append(result)
                if result.get('id') == identifier:
                    for event in deferred:
                        messages.put(event)
                    if 'error' in result:
                        raise RuntimeError(result['error']['message'])
                    return result.get('result', {})
        try:
            call(1, 'initialize', {'clientInfo': {'name': 'chat_tree_history', 'version': '1'},
                                   'capabilities': {'experimentalApi': True}})
            send({'jsonrpc': '2.0', 'method': 'initialized'})
            yield call, messages
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            reader.join(timeout=1)
            process.stdout.close()

    def request(self, method, params):
        if method not in ('thread/read', 'thread/delete', 'hooks/list'):
            raise ValueError('Unsupported persisted-history operation')
        with self.connection() as (call, messages):
            return call(2, method, params)

    def inspect(self, chat_ids):
        """Current title and archive state of each chat; None for a chat that no longer exists in Codex.

        Archived chats stay readable. A chat that fails for another reason is left out, so callers change nothing.
        """
        found = {}
        if not chat_ids:
            return found
        with self.connection() as (call, messages):
            for identifier, chat_id in enumerate(chat_ids, 2):
                try:
                    thread = call(identifier, 'thread/read', {'threadId': chat_id, 'includeTurns': False})['thread']
                except RuntimeError as error:
                    if str(error) == 'thread not loaded: ' + chat_id:
                        found[chat_id] = None
                    continue
                found[chat_id] = {'title': thread.get('name') or '',
                                  'archived': '/archived_sessions/' in (thread.get('path') or '')}
        return found

    def hooks_trusted(self):
        """True when every Chat Tree lifecycle hook is enabled and trusted, so hooks will track new chats."""
        hooks = [h for group in self.request('hooks/list', {}).get('data', []) for h in group.get('hooks', [])
                 if (h.get('pluginId') or '').startswith('chat-tree@')]
        active = {h.get('eventName') for h in hooks if h.get('enabled') and h.get('trustStatus') == 'trusted'}
        return {'userPromptSubmit', 'stop', 'interrupt', 'sessionEnd'} <= active

    @staticmethod
    def _settings(thread):
        """Reconstruct persisted settings, including later per-turn updates."""
        settings = meta = None
        context_fields = {'cwd': 'cwd', 'model': 'model', 'effort': 'reasoning_effort',
                          'summary': 'reasoning_summary', 'workspace_roots': 'runtime_workspace_roots',
                          'approval_policy': 'approval_policy', 'approvals_reviewer': 'approvals_reviewer',
                          'permission_profile': 'permission_profile', 'active_permission_profile': 'active_permission_profile',
                          'collaboration_mode': 'collaboration_mode', 'personality': 'personality',
                          'disabled_plugin_ids': 'disabled_plugin_ids'}
        with Path(thread['path']).open() as stream:
            for line in stream:
                item = json.loads(line)
                payload = item['payload']
                if item['type'] == 'session_meta' and payload['id'] == thread['id']:
                    meta = payload
                elif item['type'] == 'event_msg' and payload.get('type') == 'thread_settings_applied':
                    # A later snapshot may omit a field (resuming a chat writes one without reasoning_summary);
                    # an omitted field keeps its earlier value.
                    settings = {**(settings or {}), **payload['thread_settings']}
                elif item['type'] == 'turn_context' and settings is not None:
                    settings.update({target: payload[source] for source, target in context_fields.items() if source in payload})
        required = {'cwd', 'model', 'model_provider_id', 'reasoning_effort', 'reasoning_summary',
                    'runtime_workspace_roots', 'approval_policy', 'approvals_reviewer', 'permission_profile',
                    'active_permission_profile', 'collaboration_mode', 'personality', 'disabled_plugin_ids', 'service_tier'}
        if meta is None or settings is None or required - settings.keys():
            raise RuntimeError('Codex has not persisted a complete parent settings snapshot; inheritance cannot be guaranteed')
        if not settings['model'] or not settings['model_provider_id'] or not Path(settings['cwd']).is_dir():
            raise RuntimeError('Parent model, provider or workspace is unavailable')
        return settings, meta['base_instructions']

    def create(self, parent_chat_id, saved, title=None, reserve=None):
        """Fork settings before the first turn without starting a model turn.

        Persistent forks materialize their rollout immediately, so a chat without turns is already saved.
        """
        with self.connection() as (call, messages):
            parent = call(2, 'thread/read', {'threadId': parent_chat_id, 'includeTurns': False})['thread']
            settings, instructions = self._settings(parent)
            first = call(3, 'thread/turns/list', {'threadId': parent_chat_id, 'limit': 1, 'sortDirection': 'asc'})['data']
            if not first:
                raise RuntimeError('Parent chat has no persisted turn to fork before')
            params = {'threadId': parent_chat_id, 'beforeTurnId': first[0]['id'],
                      'excludeTurns': True, 'ephemeral': False, 'model': settings['model'],
                      'modelProvider': settings['model_provider_id'], 'cwd': settings['cwd'],
                      'runtimeWorkspaceRoots': settings['runtime_workspace_roots'],
                      'approvalPolicy': settings['approval_policy'], 'approvalsReviewer': settings['approvals_reviewer'],
                      'serviceTier': settings['service_tier']}
            if settings['reasoning_effort'] is not None:
                params['config'] = {'model_reasoning_effort': settings['reasoning_effort']}
            if settings['active_permission_profile'] is not None:
                params['permissions'] = settings['active_permission_profile']['id']
            if reserve:
                reserve()
            created = call(4, 'thread/fork', params)['thread']
            saved(created['id'])  # Record the UUID before starting any model work.
            if title:
                call(5, 'thread/name/set', {'threadId': created['id'], 'name': title})
            call(6, 'thread/settings/update', {'threadId': created['id'],
                  'collaborationMode': settings['collaboration_mode'], 'personality': settings['personality'],
                  'summary': settings['reasoning_summary'], 'disabledPluginIds': settings['disabled_plugin_ids']})
            deadline = time.monotonic() + 20
            while True:
                applied = self._next_event(messages, deadline, 'inherited settings update')
                if applied.get('method') == 'thread/settings/updated' and applied.get('params', {}).get('threadId') == created['id']:
                    break
            child = call(7, 'thread/read', {'threadId': created['id'], 'includeTurns': True})['thread']
            actual, child_instructions = self._settings(child)
            mismatches = [field for field, value in settings.items() if actual.get(field) != value]
            if child['projectId'] != parent['projectId']:
                mismatches.append('projectId')
            if child_instructions != instructions:
                mismatches.append('base_instructions')
            if child['turns']:
                mismatches.append('parent_history')
            if mismatches:
                raise RuntimeError('Parent settings were not inherited: ' + ', '.join(mismatches))
            latest_parent = call(8, 'thread/read', {'threadId': parent_chat_id, 'includeTurns': False})['thread']
            if self._settings(latest_parent) != (settings, instructions) or latest_parent['projectId'] != parent['projectId']:
                raise RuntimeError('Parent settings changed during creation; inspect the recorded child UUID')
            return {'chat_id': created['id'], 'turn_id': None}

    def open(self, chat_id, text, instructions):
        """Run one opening turn in a new chat that Desktop has not opened yet; Desktop shows only turns.

        `text` is the user message the chat shows; `instructions` reach the model as history the user does not see.
        Returns the turn ID, or None when the turn could not start. A failure leaves the chat usable.
        """
        with self.connection() as (call, messages):
            try:
                call(2, 'thread/resume', {'threadId': chat_id})
                call(3, 'thread/inject_items', {'threadId': chat_id, 'items': [
                    {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': instructions}]}]})
                turn_id = call(4, 'turn/start', {'threadId': chat_id, 'input': [{'type': 'text', 'text': text}]})['turn']['id']
            except RuntimeError:
                return None
            deadline = time.monotonic() + 90
            try:
                while True:
                    event = self._next_event(messages, deadline, 'opening turn')
                    if event.get('method') == 'turn/completed' and event.get('params', {}).get('threadId') == chat_id:
                        return turn_id
            except RuntimeError:
                try:
                    call(5, 'turn/interrupt', {'threadId': chat_id, 'turnId': turn_id})
                except RuntimeError:
                    pass
                return turn_id
