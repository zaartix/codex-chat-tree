#!/usr/bin/env python3
"""Local stdio MCP. Browser preview uses the same dispatcher and isolated data."""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from runtime import CodexHistory
from store import Store, TreeError, encode

ROOT = Path(__file__).resolve().parent
VERSION = json.loads((ROOT / '.codex-plugin' / 'plugin.json').read_text())['version']
# A new URI per release makes hosts load the released panel.
UI_URI = 'ui://chat-tree/v' + VERSION + '/tree.html'
RECONCILE_SECONDS = 10


def caller(params):
    meta = params.get('_meta') or {}
    turn = meta.get('x-codex-turn-metadata') or {}
    if isinstance(turn, str):
        try:
            turn = json.loads(turn)
        except ValueError:
            turn = {}
    thread_id = meta.get('threadId') or meta.get('thread_id') or turn.get('thread_id')
    return {'chat_id': thread_id, 'turn_id': turn.get('turn_id'),
            'ui': bool(thread_id and 'x-codex-turn-metadata' not in meta and not meta.get('callId'))}


def definition(name, title, description, properties=None, required=None, readonly=True, ui=False, app_only=False):
    result = {'name': name, 'title': title, 'description': description,
              'inputSchema': {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False},
              'annotations': {'readOnlyHint': readonly, 'destructiveHint': name in ('tree_change','tree_ui','tree_step','tree_delete_saved_chat'), 'openWorldHint': name in ('tree_create_saved_chat','tree_delete_saved_chat')}}
    if ui:
        result['_meta'] = {'ui': {'resourceUri': UI_URI}}
        if ui == 'entrypoint':
            result['_meta']['openai/ui'] = {'entrypoints': [{'type': 'thread'}, {'type': 'global'}]}
    if app_only:
        result.setdefault('_meta', {})['ui'] = {'visibility': ['app']}
    return result


STRING = {'type': 'string'}
CHANGE = {'type': 'object', 'description': 'action=create/add/edit/move/start/finish/reopen/delete/rebuild. create only in an unlinked chat: title,common_context,items[{title,description,context}]. Inherit parent chat settings; cwd/project_id overrides are not allowed. In a linked chat use add: parent_id=current_node_id,items; set shared context via edit common_context. edit: node_id and changed text fields. move: node_id,destination_id; only branches without chats (started chats keep their parent settings, rebuild instead). Other actions: node_id. Preserve the original language of task text.'}
JOB = {'operation_id': STRING, 'token': STRING}
TOOLS = [
    definition('tree_panel', 'Chat Tree', 'Open the Chat Tree panel with the current chat checklist and navigation. Use for requested Chat Tree work.', {'focus_id': STRING, 'root_id': STRING}, ui='entrypoint'),
    definition('tree_read', 'Read Chat Tree', 'Read the Chat Tree graph, contexts and operations without changing the tree.', {'focus_id': STRING, 'root_id': STRING}),
    definition('tree_propose', 'Propose a change', 'Prepare a specific change for user approval without modifying the tree.', {'change': CHANGE}, ['change'], readonly=False),
    definition('tree_change', 'Change Chat Tree', 'Change the Chat Tree graph at the explicit request of the user. Use for requested Chat Tree work; clarify ambiguity or use tree_propose for panel approval. Follow the chat-tree skill. Preserve the original language of task text.',
               {'change': CHANGE, 'expected_revision': {'type': 'integer'}}, ['change'], readonly=False),
    definition('tree_ui', 'User action', 'A direct user action in the panel. Not available to the model.', {'change': CHANGE, 'proposal_id': STRING, 'dismiss_proposal': STRING, 'expected_revision': {'type': 'integer'}, **JOB, 'step': {'type':'string','enum':['cancel']}}, readonly=False, app_only=True),
    definition('tree_job', 'Branch operation', 'Read an approved operation and retrieve its exact summary/delivery service prompt. Do not create another chat when the operation is creating or has a chat_id.', {**JOB, 'node_id': STRING, 'purpose': {'type': 'string', 'enum': ['summary', 'delivery']}}, ['operation_id', 'token']),
    definition('tree_create_saved_chat', 'Create an item chat', 'Create exactly one approved chat by forking the immediate parent settings without its work history. The server verifies inheritance and binds the chat; no model turn runs, and the branch context reaches the chat with its first message. Available to the start/rebuild initiator. Open the parent chat first. Then open the saved chat in Desktop. Do not retry creation after an error: the UUID is saved immediately.', {**JOB, 'node_id': STRING}, ['operation_id','token','node_id'], readonly=False),
    definition('tree_summary', 'Save a fresh summary', 'Save one summary paragraph for the current chat during approved collection. Save immediate child summaries first. Preserve the chat working language. Does not complete other branches.', {**JOB, 'summary': STRING}, ['operation_id', 'token', 'summary'], readonly=False),
    definition('tree_step', 'Continue operation', 'Advance an approved operation from the initiating chat: claim,prepare_replacement,ready_to_delete,delivered,commit,cancel,error. Follow the chat-tree skill; commit validates completion.', {**JOB, 'step': {'type': 'string', 'enum': ['claim','prepare_replacement','ready_to_delete','delivered','commit','cancel','error']}, 'data': {'type': 'object'}}, ['operation_id','token','step'], readonly=False),
    definition('tree_delete_saved_chat', 'Delete an archived chat', 'Permanently delete one chat from an approved delete/rebuild operation after native archive, using public thread/delete. Requires protective hooks and verifies the branch reservation. Do not bypass an active writer refusal.', {**JOB, 'node_id': STRING}, ['operation_id','token','node_id'], readonly=False),
]


class Dispatcher:
    def __init__(self, store=None, history=None):
        # An incompatible database must not stop the server: every tool call reports it instead.
        self.unavailable = None
        try:
            self.store = store or Store()
        except TreeError as error:
            self.store, self.unavailable = None, error
        self.history = history or CodexHistory()
        self.reconciled = None

    def reconcile(self):
        """Forget items whose chats were deleted in Codex. Throttled; any failure leaves the tree unchanged."""
        if self.reconciled is not None and time.monotonic() - self.reconciled < RECONCILE_SECONDS:
            return
        self.reconciled = time.monotonic()
        try:
            gone = self.history.deleted(self.store.linked_chats())
        except Exception:
            return
        if gone:
            self.store.forget_chats(gone)

    def _creation(self, change, chat_id):
        if change.get('action') != 'create':
            return change
        if not chat_id:
            raise TreeError('no_chat', 'The host did not provide a chat UUID')
        if set(change) - {'action', 'title', 'description', 'context', 'common_context', 'items', 'cwd', 'project_id'}:
            raise TreeError('invalid_input', 'Chat settings are inherited from the parent; extra parameters are not allowed')
        saved = self.history.request('thread/read', {'threadId': chat_id, 'includeTurns': False})['thread']
        inherited = {'cwd': saved['cwd'], 'project_id': saved['projectId']}
        for field, value in inherited.items():
            if field in change and change[field] != value:
                raise TreeError('invalid_input', 'Chat settings are inherited from the parent; changing '+field+' is not allowed')
        return {**change, **inherited}

    def call(self, name, args, actor):
        chat_id = actor['chat_id']
        if name in ('tree_panel', 'tree_read'):
            self.reconcile()
            return self.store.view(chat_id, args.get('focus_id'), args.get('root_id'))
        if name == 'tree_propose':
            return self.store.propose(self._creation(args['change'], chat_id),chat_id)
        if name == 'tree_change':
            if not chat_id:
                raise TreeError('no_chat', 'The host did not provide a chat UUID')
            if actor['ui']:
                raise TreeError('denied', 'Panel actions must use tree_ui')
            context = {'source': 'agent_tool', 'chat_id': chat_id, 'turn_id': actor['turn_id']}
            return self.store.apply(self._creation(args['change'], chat_id), context, chat_id, args.get('expected_revision'))
        if name == 'tree_ui':
            if not actor['ui']:
                raise TreeError('denied', 'Only a user action in the panel may call this tool')
            if args.get('step') == 'cancel':
                return self.store.advance(args['operation_id'],args['token'],'cancel',actor_chat=chat_id)
            if args.get('dismiss_proposal'):
                return self.store.dismiss(args['dismiss_proposal'])
            return self.store.apply(self._creation(args.get('change', {}), chat_id), {'source': 'user_interface', 'chat_id': chat_id}, chat_id,
                                    args.get('expected_revision'), args.get('proposal_id'))
        if name == 'tree_job':
            if args.get('purpose'):
                return self.store.service_prompt(args['operation_id'], args['token'], args['node_id'], args['purpose'])
            return self.store.job(args['operation_id'], args['token'])
        if name == 'tree_create_saved_chat':
            op=self.store.job(args['operation_id'],args['token'])
            if op['payload']['actor_chat']!=chat_id:
                raise TreeError('denied','Only the initiating chat may perform this operation')
            if actor['ui'] and op['stage']=='requested':
                self.store.advance(op['id'],args['token'],'claim',actor_chat=chat_id)
            try:
                with self.store.connect() as db:
                    node = self.store.node(db, args['node_id'])
                    parent = self.store.node(db, node['parent_id'])
                # A persistent fork is saved without a turn. No model turn runs: the server binds the chat it created,
                # and the prompt hook delivers the branch context with the user's first message.
                created = self.history.create(parent['chat_id'],
                    lambda uuid: self.store.created(args['operation_id'], args['token'], args['node_id'], uuid), title=node['title'],
                    reserve=lambda: self.store.reserve_creation(args['operation_id'], args['token'], args['node_id'], chat_id))
                self.store.bind(args['operation_id'], args['token'], created['chat_id'], args['node_id'])
                try:
                    trusted = self.history.hooks_trusted()
                except Exception:
                    trusted = False
                if trusted:
                    self.store.fresh_chat(created['chat_id'])
                return {**created, 'node_id': node['id'], 'title': node['title'], 'url': 'codex://threads/'+created['chat_id']}
            except Exception as error:
                self.store.advance(args['operation_id'], args['token'], 'error', {'message': str(error)}, chat_id)
                raise RuntimeError(str(error)) from error
        if name == 'tree_summary':
            view = self.store.view(chat_id)
            if not view['current_node_id'] or actor['ui']:
                raise TreeError('denied', 'Only the agent in a linked chat may save its summary')
            op = self.store.job(args['operation_id'], args['token'])
            if chat_id != op['payload']['actor_chat']:
                self.store.check_service(args['operation_id'],args['token'],view['current_node_id'],
                                         self.history.user_prompt(chat_id,actor['turn_id']),'summary')
            return self.store.record_summary(args['operation_id'], args['token'], view['current_node_id'], args['summary'], chat_id, actor['turn_id'])
        if name == 'tree_step':
            if actor['ui']:
                raise TreeError('denied', 'Only the initiating chat agent may advance operation steps')
            if args['step'] == 'delivered':
                op = self.store.job(args['operation_id'], args['token'])
                node = self.store.view(chat_id, op['node_id'])['nodes']
                selected = next(n for n in node if n['id'] == op['node_id'])
                if not selected['parent_id']:
                    raise TreeError('invalid_step', 'The root task has no parent')
                parent = next(n for n in node if n['id'] == selected['parent_id'])
                if parent['chat_id'] != chat_id:
                    prompt = self.history.user_prompt(parent['chat_id'], (args.get('data') or {}).get('turn_id'))
                    self.store.check_service(args['operation_id'],args['token'],parent['id'],prompt,'delivery')
            return self.store.advance(args['operation_id'], args['token'], args['step'], args.get('data'), chat_id)
        if name == 'tree_delete_saved_chat':
            op = self.store.job(args['operation_id'], args['token'])
            if op['stage'] != 'deleting' or chat_id != op['payload']['actor_chat'] or op['kind'] not in ('delete','rebuild'):
                raise TreeError('denied', 'Deletion is not allowed by this operation')
            node = next((n for n in op['payload']['nodes'] if n['id'] == args['node_id']), None)
            if not node or not node['chat_id'] or node['chat_id'] == chat_id:
                raise TreeError('denied', 'This chat is outside the deletion scope')
            if node['id'] in op['payload']['completed']:
                return op
            with self.store.connect() as db:
                self.store._idle(db, [node], chat_id)
                lock = db.execute('SELECT operation_id FROM locks WHERE node_id=?', (node['id'],)).fetchone()
                if not lock or lock['operation_id'] != op['id']:
                    raise TreeError('denied', 'The branch has not been reserved')
            self.history.request('thread/delete', {'threadId': node['chat_id']})
            return self.store.advance(op['id'], args['token'], 'deleted', {'node_id': node['id']}, chat_id)
        raise TreeError('unknown_tool', 'Unknown tool')

    def handle(self, method, params):
        if method == 'initialize':
            return {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                    'capabilities': {'tools': {}, 'resources': {}}, 'serverInfo': {'name': 'chat-tree', 'version': VERSION}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': TOOLS}
        if method == 'resources/list':
            return {'resources': [{'uri': UI_URI, 'name': 'Chat Tree', 'mimeType': 'text/html;profile=mcp-app'}]}
        if method == 'resources/read':
            if params.get('uri') != UI_URI:
                raise TreeError('not_found', 'Resource not found')
            return {'contents': [{'uri': UI_URI, 'mimeType': 'text/html;profile=mcp-app',
                                  'text': (ROOT / 'assets' / 'tree.html').read_text().replace('__VERSION__', VERSION), '_meta': {'ui': {'prefersBorder': False}}}]}
        if method == 'tools/call':
            name, args = params['name'], params.get('arguments') or {}
            tool = next((t for t in TOOLS if t['name'] == name), None)
            if not tool or set(args) - set(tool['inputSchema']['properties']) or set(tool['inputSchema']['required']) - set(args):
                raise TreeError('invalid_input', 'Invalid tool arguments')
            try:
                if self.unavailable:
                    raise self.unavailable
                actor=caller(params)
                if not actor['ui']:
                    self.store.observe_turn(actor['chat_id'],actor['turn_id'])
                result = self.call(name, args, actor)
                # A create/bind call can establish the chat association itself.
                if not actor['ui']:
                    self.store.observe_turn(actor['chat_id'],actor['turn_id'])
                return {'structuredContent': result, 'content': [{'type': 'text', 'text': encode(result)}]}
            except (TreeError, RuntimeError) as error:
                result = error.result() if isinstance(error, TreeError) else {'error': {'code': 'runtime_error', 'message': str(error)}}
                return {'isError': True, 'structuredContent': result, 'content': [{'type': 'text', 'text': encode(result)}]}
        raise TreeError('unsupported', 'Unsupported method')


def main():
    dispatcher = Dispatcher()
    output=threading.Lock()
    def respond(request):
        try:
            result = dispatcher.handle(request['method'], request.get('params') or {})
            reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except Exception as error:
            reply = {'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32603, 'message': str(error)}}
        with output:
            print(json.dumps(reply, ensure_ascii=False), flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for line in sys.stdin:
            try:
                request=json.loads(line)
                if 'id' in request:
                    pool.submit(respond,request)
            except ValueError:
                print('Invalid JSON request', file=sys.stderr)


if __name__ == '__main__':
    main()
