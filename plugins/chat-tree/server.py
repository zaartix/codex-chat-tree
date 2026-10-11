#!/usr/bin/env python3
"""Local stdio MCP. Browser preview uses the same dispatcher and isolated data."""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from outline import outline
from store import Store, TreeError, encode, host_name

ROOT = Path(__file__).resolve().parent
VERSION = json.loads((ROOT / '.codex-plugin' / 'plugin.json').read_text())['version']
# A new URI per release makes hosts load the released panel.
UI_URI = 'ui://chat-tree/v' + VERSION + '/tree.html'
RECONCILE_SECONDS = 10
OPENING = ('Chat Tree opened this item chat. The next user message is the card of this item, not a request to work. '
           'Call tree_panel once so the user sees this item and its parent, then reply with one short line in the language '
           'of the item saying the chat is ready for this item. Do not start the task, read files or change the tree.')
CLAUDE_BACKGROUND = ('This session is a Chat Tree item chat. Chat Tree adds the task context of the item to every message here. '
                     'Present the item and wait for the user; do not start the task on your own.')


def default_host():
    if host_name() == 'claude':
        from claude import ClaudeDesktop
        return ClaudeDesktop()
    from runtime import CodexHistory
    return CodexHistory()


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
CODEX_TOOLS = [
    definition('tree_panel', 'Chat Tree', 'Open the Chat Tree panel with the current chat checklist and navigation. Use for requested Chat Tree work.', {'focus_id': STRING, 'root_id': STRING}, ui='entrypoint'),
    definition('tree_read', 'Read Chat Tree', 'Read the Chat Tree graph, contexts and operations without changing the tree.', {'focus_id': STRING, 'root_id': STRING}),
    definition('tree_propose', 'Propose a change', 'Prepare a specific change for user approval without modifying the tree.', {'change': CHANGE}, ['change'], readonly=False),
    definition('tree_change', 'Change Chat Tree', 'Change the Chat Tree graph at the explicit request of the user. Use for requested Chat Tree work; clarify ambiguity or use tree_propose for panel approval. Follow the chat-tree skill. Preserve the original language of task text.',
               {'change': CHANGE, 'expected_revision': {'type': 'integer'}}, ['change'], readonly=False),
    definition('tree_ui', 'User action', 'A direct user action in the panel. Not available to the model.', {'change': CHANGE, 'proposal_id': STRING, 'dismiss_proposal': STRING, 'expected_revision': {'type': 'integer'}, **JOB, 'step': {'type':'string','enum':['cancel']}}, readonly=False, app_only=True),
    definition('tree_job', 'Branch operation', 'Read an approved operation and retrieve its exact summary/delivery service prompt: summary needs the node_id of the item, delivery goes to the immediate parent of the selected item. Do not create another chat when the operation is creating or has a chat_id.', {**JOB, 'node_id': STRING, 'purpose': {'type': 'string', 'enum': ['summary', 'delivery']}}, ['operation_id', 'token']),
    definition('tree_create_saved_chat', 'Create an item chat', 'Create exactly one approved chat by forking the immediate parent settings without its work history. The server verifies inheritance and binds the chat; no model turn runs, and the branch context reaches the chat with its first message. Available to the start/rebuild initiator. Open the parent chat first. Then open the saved chat in Desktop. Do not retry creation after an error: the UUID is saved immediately.', {**JOB, 'node_id': STRING}, ['operation_id','token','node_id'], readonly=False),
    definition('tree_summary', 'Save a fresh summary', 'Save one summary paragraph for the current chat during approved collection. Save immediate child summaries first. Preserve the chat working language. Does not complete other branches.', {**JOB, 'summary': STRING}, ['operation_id', 'token', 'summary'], readonly=False),
    definition('tree_step', 'Continue operation', 'Advance an approved operation from the initiating chat: claim,prepare_replacement,ready_to_delete,delivered,commit,cancel,error. Follow the chat-tree skill; commit validates completion.', {**JOB, 'step': {'type': 'string', 'enum': ['claim','prepare_replacement','ready_to_delete','delivered','commit','cancel','error']}, 'data': {'type': 'object'}}, ['operation_id','token','step'], readonly=False),
    definition('tree_delete_saved_chat', 'Delete an archived chat', 'Permanently delete one chat from an approved delete/rebuild operation after native archive, using public thread/delete. Requires protective hooks and verifies the branch reservation. Do not bypass an active writer refusal.', {**JOB, 'node_id': STRING}, ['operation_id','token','node_id'], readonly=False),
]
# Claude Desktop shows no MCP app panel and only its agent manages sessions: the server prepares and verifies.
CLAUDE_TOOLS = [
    definition('tree_panel', 'Chat Tree', 'Show the Chat Tree checklist of the current chat. Reply with `outline` exactly as returned: it is Markdown with links that open the item chats. Use for requested Chat Tree work.', {'focus_id': STRING, 'root_id': STRING}),
    definition('tree_read', 'Read Chat Tree', 'Read the Chat Tree graph, contexts and operations without changing the tree.', {'focus_id': STRING, 'root_id': STRING}),
    definition('tree_change', 'Change Chat Tree', 'Change the Chat Tree graph at the explicit request of the user. Use for requested Chat Tree work; clarify ambiguity first. Follow the chat-tree skill. Preserve the original language of task text.',
               {'change': CHANGE, 'expected_revision': {'type': 'integer'}}, ['change'], readonly=False),
    definition('tree_job', 'Branch operation', 'Read an approved operation and retrieve its exact summary/delivery service prompt: summary needs the node_id of the item, delivery goes to the immediate parent of the selected item. With wait_for, wait up to five minutes until that item has saved its summary. Do not create another chat when the operation is creating or has a chat_id.',
               {**JOB, 'node_id': STRING, 'purpose': {'type': 'string', 'enum': ['summary', 'delivery']}, 'wait_for': STRING}, ['operation_id', 'token']),
    definition('tree_create_saved_chat', 'Prepare an item chat', 'Reserve exactly one approved item chat and return the start_session arguments that create it. Call it in the chat of the immediate parent, so the new chat inherits its settings. Then call start_session with exactly these arguments and report the new session ID with tree_step started. Calling it again before the chat exists returns the same arguments.', {**JOB, 'node_id': STRING}, ['operation_id','token','node_id'], readonly=False),
    definition('tree_summary', 'Save a fresh summary', 'Save one summary paragraph for the current chat during approved collection. Save immediate child summaries first. Preserve the chat working language. Does not complete other branches.', {**JOB, 'summary': STRING}, ['operation_id', 'token', 'summary'], readonly=False),
    definition('tree_step', 'Continue operation', 'Advance an approved operation from the initiating chat: claim,started,prepare_replacement,ready_to_delete,delivered,commit,cancel,error. started takes data {node_id, chat_id} with the session ID from start_session and verifies inheritance. Follow the chat-tree skill; commit validates completion.', {**JOB, 'step': {'type': 'string', 'enum': ['claim','started','prepare_replacement','ready_to_delete','delivered','commit','cancel','error']}, 'data': {'type': 'object'}}, ['operation_id','token','step'], readonly=False),
    definition('tree_delete_saved_chat', 'Record a deleted chat', 'Record one chat of an approved delete/rebuild operation after you archived it and deleted it with delete_session. Verifies that Claude no longer has the session and the branch reservation.', {**JOB, 'node_id': STRING}, ['operation_id','token','node_id'], readonly=False),
]
TOOLS = CODEX_TOOLS


class Dispatcher:
    def __init__(self, store=None, history=None):
        """`history` is the host: Codex app-server history, or Claude Desktop."""
        self.history = history or default_host()
        self.claude = getattr(self.history, 'name', 'codex') == 'claude'
        self.tools = CLAUDE_TOOLS if self.claude else CODEX_TOOLS
        # An incompatible database must not stop the server: every tool call reports it instead.
        self.unavailable = None
        try:
            self.store = store or (Store(link=self.history.link_to, live=self.history.live, parent_creates=True) if self.claude else Store())
        except TreeError as error:
            self.store, self.unavailable = None, error
        self.reconciled = None
        self.pending = []

    def reconcile(self, force=False):
        """Compare Codex chats with the tree: forget deleted chats, close branches the user archived and list the
        native actions (archive, unarchive, rename) the agent must apply. Throttled unless forced."""
        if not force and self.reconciled is not None and time.monotonic() - self.reconciled < RECONCILE_SECONDS:
            return self.pending
        self.reconciled = time.monotonic()
        try:
            chats = self.history.inspect(self.store.linked_chats())
            gone = [chat_id for chat_id, chat in chats.items() if chat is None]
            if gone:
                self.store.forget_chats(gone)
            self.pending = self.store.sync(chats)
        except Exception:
            pass
        return self.pending

    def _creation(self, change, chat_id):
        if change.get('action') != 'create':
            return change
        if not chat_id:
            raise TreeError('no_chat', 'The host did not provide a chat UUID')
        if set(change) - {'action', 'title', 'description', 'context', 'common_context', 'items', 'cwd', 'project_id'}:
            raise TreeError('invalid_input', 'Chat settings are inherited from the parent; extra parameters are not allowed')
        if self.claude:
            inherited = {'cwd': self.history.settings(chat_id)['cwd'] or '', 'project_id': None}
        else:
            saved = self.history.request('thread/read', {'threadId': chat_id, 'includeTurns': False})['thread']
            inherited = {'cwd': saved['cwd'], 'project_id': saved['projectId']}
        for field, value in inherited.items():
            if field in change and change[field] != value:
                raise TreeError('invalid_input', 'Chat settings are inherited from the parent; changing '+field+' is not allowed')
        return {**change, **inherited}

    def call(self, name, args, actor):
        chat_id = actor['chat_id']
        if name in ('tree_panel', 'tree_read'):
            self.reconcile(force=not actor['ui'])
            view = self.store.view(chat_id, args.get('focus_id'), args.get('root_id'))
            if self.claude and name == 'tree_panel':
                return {'outline': outline(view, self.history.link_to), 'current_node_id': view['current_node_id'],
                        'focus_id': view['focus_id'], 'revision': view['revision']}
            return view
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
            if args.get('wait_for'):
                return self._wait_summary(args['operation_id'], args['token'], args['wait_for'])
            if args.get('purpose'):
                return self.store.service_prompt(args['operation_id'], args['token'], args.get('node_id'), args['purpose'])
            return self.store.job(args['operation_id'], args['token'])
        if name == 'tree_create_saved_chat' and self.claude:
            return self._prepare_chat(args, chat_id)
        if name == 'tree_step' and self.claude and args['step'] == 'started':
            return self._started(args, chat_id)
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
                title = self.store.chat_title(node['id'])
                created = self.history.create(parent['chat_id'],
                    lambda uuid: self.store.created(args['operation_id'], args['token'], args['node_id'], uuid), title=title,
                    reserve=lambda: self.store.reserve_creation(args['operation_id'], args['token'], args['node_id'], chat_id))
                self.store.bind(args['operation_id'], args['token'], created['chat_id'], args['node_id'])
                try:
                    trusted = self.history.hooks_trusted()
                except Exception:
                    trusted = False
                if trusted:
                    self.store.fresh_chat(created['chat_id'])
                # Desktop shows only turns, so once the chat is bound one short opening turn presents the item:
                # its card is the user message and the agent answers with the panel of this item.
                card = '\n\n'.join(part for part in (title, node['description']) if part)
                try:
                    created['turn_id'] = self.history.open(created['chat_id'], card, OPENING)
                except Exception:
                    pass  # The chat is created and bound; it only opens without the card.
                return {**created, 'node_id': node['id'], 'title': node['title'], 'url': 'codex://threads/'+created['chat_id']}
            except Exception as error:
                self.store.advance(args['operation_id'], args['token'], 'error', {'message': str(error)}, chat_id)
                raise RuntimeError(str(error)) from error
        if name == 'tree_summary':
            view = self.store.view(chat_id)
            if not view['current_node_id'] or actor['ui']:
                raise TreeError('denied', 'Only the agent in a linked chat may save its summary')
            return self.store.record_summary(args['operation_id'], args['token'], view['current_node_id'], args['summary'], chat_id, actor['turn_id'])
        if name == 'tree_step':
            if actor['ui']:
                raise TreeError('denied', 'Only the initiating chat agent may advance operation steps')
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
            if self.claude:
                if self.history.record(node['chat_id']) is not None:
                    raise TreeError('not_deleted', 'Claude still has this session. Archive it, delete it with delete_session, then record it again',
                                    chat_id=node['chat_id'], url=self.history.link_to(node['chat_id']))
                return self.store.advance(op['id'], args['token'], 'deleted', {'node_id': node['id']}, chat_id)
            with self.store.connect() as db:
                self.store._idle(db, [node], chat_id)
            self.history.request('thread/delete', {'threadId': node['chat_id']})
            return self.store.advance(op['id'], args['token'], 'deleted', {'node_id': node['id']}, chat_id)
        raise TreeError('unknown_tool', 'Unknown tool')

    def _wait_summary(self, operation_id, token, node_id, seconds=300):
        """Claude delivers a service message without waiting for its answer, so the initiator waits for the result."""
        deadline = time.monotonic() + seconds
        while True:
            op = self.store.job(operation_id, token)
            if node_id in op['summaries'] or op['stage'] in ('done', 'cancelled') or time.monotonic() >= deadline:
                return {**op, 'waited_for': node_id, 'saved': node_id in op['summaries']}
            time.sleep(2)

    def _prepare_chat(self, args, chat_id):
        op = self.store.job(args['operation_id'], args['token'])
        if op['payload']['actor_chat'] != chat_id:
            raise TreeError('denied', 'Only the initiating chat may perform this operation')
        with self.store.connect() as db:
            node = self.store.node(db, args['node_id'])
            parent = self.store.node(db, node['parent_id']) if node['parent_id'] else None
        # start_session gives the new session the settings of the session that calls it.
        if not parent or parent['chat_id'] != chat_id:
            raise TreeError('open_parent', 'Create this item chat from the chat of its immediate parent so it inherits the parent settings',
                            chat_id=parent and parent['chat_id'], url=parent and parent['chat_id'] and self.history.link_to(parent['chat_id']))
        self.store.reserve_creation(args['operation_id'], args['token'], args['node_id'], chat_id, repeat=True)
        title = self.store.chat_title(node['id'])
        card = '\n\n'.join(part for part in (title, node['description']) if part)
        return {'node_id': node['id'], 'title': title, 'start_session': {
            'initiation': 'user_asked', 'context': 'fresh', 'use_worktree': False, 'title': title,
            'prompt': card + '\n\n— Chat Tree ' + args['token'], 'background': CLAUDE_BACKGROUND}}

    def _started(self, args, chat_id):
        """Bind the session start_session created, unless its first prompt already did, and verify inheritance."""
        data = args.get('data') or {}
        node_id, child = data.get('node_id'), data.get('chat_id')
        op = self.store.job(args['operation_id'], args['token'])
        if op['payload']['actor_chat'] != chat_id:
            raise TreeError('denied', 'Only the initiating chat may perform this operation')
        if node_id not in op['payload'].get('creations', {}):
            raise TreeError('denied', 'Chat creation has not been reserved for this item')
        if not self.history.wait_record(child):
            raise TreeError('not_found', 'Claude Desktop has no session ' + str(child) + '; report the start_session result')
        with self.store.connect() as db:
            node = self.store.node(db, node_id)
            parent = self.store.node(db, node['parent_id'])
        if node['chat_id'] != child:
            self.store.created(args['operation_id'], args['token'], node_id, child)
            self.store.bind(args['operation_id'], args['token'], child, node_id)
        expected, actual = self.history.settings(parent['chat_id']), self.history.settings(child)
        mismatches = [field for field, value in expected.items() if actual.get(field) != value]
        if mismatches:
            message = 'Parent settings were not inherited: ' + ', '.join(mismatches)
            self.store.advance(args['operation_id'], args['token'], 'error', {'message': message}, chat_id)
            raise TreeError('not_inherited', message, chat_id=child, url=self.history.link_to(child), expected=expected, actual=actual)
        return {'node_id': node_id, 'chat_id': child, 'url': self.history.link_to(child), 'title': node['title'],
                'operation': self.store.job(args['operation_id'], args['token'])}

    def handle(self, method, params):
        if method == 'initialize':
            return {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                    'capabilities': {'tools': {}, 'resources': {}}, 'serverInfo': {'name': 'chat-tree', 'version': VERSION}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': self.tools}
        if method == 'resources/list':
            if self.claude:
                return {'resources': []}
            return {'resources': [{'uri': UI_URI, 'name': 'Chat Tree', 'mimeType': 'text/html;profile=mcp-app'}]}
        if method == 'resources/read':
            if params.get('uri') != UI_URI:
                raise TreeError('not_found', 'Resource not found')
            return {'contents': [{'uri': UI_URI, 'mimeType': 'text/html;profile=mcp-app',
                                  'text': (ROOT / 'assets' / 'tree.html').read_text().replace('__VERSION__', VERSION), '_meta': {'ui': {'prefersBorder': False}}}]}
        if method == 'tools/call':
            name, args = params['name'], params.get('arguments') or {}
            tool = next((t for t in self.tools if t['name'] == name), None)
            if not tool or set(args) - set(tool['inputSchema']['properties']) or set(tool['inputSchema']['required']) - set(args):
                raise TreeError('invalid_input', 'Invalid tool arguments')
            try:
                if self.unavailable:
                    raise self.unavailable
                actor = {'chat_id': self.history.current_chat(), 'turn_id': None, 'ui': False} if self.claude else caller(params)
                if not actor['ui']:
                    self.store.observe_turn(actor['chat_id'],actor['turn_id'])
                result = self.call(name, args, actor)
                if name in ('tree_change', 'tree_ui', 'tree_step', 'tree_create_saved_chat'):
                    # Numbers shift and archive states change with the tree.
                    self.reconcile(force=True)
                if not actor['ui'] and isinstance(result, dict) and self.pending:
                    result = {**result, 'sync': self.pending}
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
