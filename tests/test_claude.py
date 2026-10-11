import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'plugins' / 'chat-tree'))
import claude
from claude import ClaudeDesktop
from outline import outline
from server import Dispatcher
from store import Store, TreeError

ROOT = 'local_00000000-0000-4000-8000-000000000000'
ITEM = 'local_11111111-1111-4111-8111-111111111111'
OTHER = 'local_22222222-2222-4222-8222-222222222222'
EVIDENCE = {'source': 'test_user_action'}


class Desktop:
    """Claude's own records in a temporary config and app directory."""

    def __init__(self, base):
        self.config, self.app = Path(base) / 'claude', Path(base) / 'app'
        self.records = self.app / 'claude-code-sessions' / 'account' / 'org'
        (self.config / 'sessions').mkdir(parents=True)
        self.records.mkdir(parents=True)
        self.host = ClaudeDesktop(self.config, self.app)

    def process(self, pid, session, chat=None, status='idle'):
        info = {'pid': pid, 'sessionId': session, 'status': status, 'kind': 'interactive'}
        if chat:
            info['hostSessionId'] = chat
        (self.config / 'sessions' / (str(pid) + '.json')).write_text(json.dumps(info))

    def session(self, chat, title='', archived=False, cwd='/work', model='claude-opus-5-5', effort='high', mode='acceptEdits'):
        (self.records / (chat + '.json')).write_text(json.dumps({
            'sessionId': chat, 'title': title, 'isArchived': archived, 'cwd': cwd, 'model': model,
            'effort': effort, 'permissionMode': mode}))

    def delete(self, chat):
        (self.records / (chat + '.json')).unlink()
        (self.records / ('deleted_' + chat[len('local_'):])).mkdir()


class ClaudeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.desktop = Desktop(self.temp.name)
        self.host = self.desktop.host

    def tearDown(self):
        self.temp.cleanup()

    def test_hook_belongs_to_the_desktop_session_of_its_process(self):
        me = os.getpid()
        self.desktop.process(me, 'cli-session', ROOT)
        self.assertEqual(self.host.hook_chat({'session_id': 'cli-session'}), ROOT)
        # A headless `claude -p` inside a session has its own session and no Desktop session.
        self.desktop.process(me, 'nested-session')
        self.assertIsNone(self.host.hook_chat({'session_id': 'nested-session'}))
        self.assertIsNone(self.host.hook_chat({'session_id': 'unknown'}))

    def test_server_belongs_to_the_session_of_its_parent_process(self):
        self.desktop.process(os.getppid(), 'cli-session', ROOT)
        self.assertEqual(self.host.current_chat(), ROOT)

    def test_processes_that_ended_do_not_count(self):
        self.desktop.process(os.getpid(), 'a', ROOT, status='busy')
        self.desktop.process(2 ** 22 + 12345, 'b', ITEM, status='busy')
        self.assertEqual(self.host.live([ROOT, ITEM, OTHER]), {ROOT: 'working', ITEM: 'idle', OTHER: 'idle'})

    def test_only_a_deletion_marker_means_a_deleted_chat(self):
        self.desktop.session(ROOT, '[1] A', archived=True)
        self.desktop.session(ITEM)
        self.desktop.delete(ITEM)
        self.assertEqual(self.host.inspect([ROOT, ITEM, OTHER]), {ROOT: {'title': '[1] A', 'archived': True}, ITEM: None})
        self.assertIsNone(self.host.record('../escape'))

    def test_settings_come_from_the_saved_session(self):
        self.desktop.session(ROOT, cwd='/repo', model='m', effort='low', mode='plan')
        self.assertEqual(self.host.settings(ROOT), {'cwd': '/repo', 'model': 'm', 'effort': 'low', 'permission_mode': 'plan'})


class ClaudeTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = {}
        self.store = Store(Path(self.temp.name) / 'tree.sqlite3', link=claude.link,
                           live=lambda ids: {c: self.state.get(c, 'idle') for c in ids}, parent_creates=True)
        r = self.store.apply({'action': 'create', 'title': 'Root', 'cwd': '/work', 'common_context': 'Shared',
                              'items': [{'title': 'A', 'description': 'Do A'}, {'title': 'B'}]}, EVIDENCE, ROOT)
        self.root, (self.a, self.b) = r['node_id'], r['item_ids']

    def tearDown(self):
        self.temp.cleanup()

    def start(self, node, actor=ROOT):
        op = self.store.apply({'action': 'start', 'node_id': node}, EVIDENCE, actor)['operation']
        self.store.advance(op['id'], op['token'], 'claim', actor_chat=actor)
        self.store.reserve_creation(op['id'], op['token'], node, actor, repeat=True)
        return op

    def test_first_prompt_with_the_token_binds_the_new_chat(self):
        op = self.start(self.a)
        self.assertIsNone(self.store.hook(ITEM, 'UserPromptSubmit', 'p1', 'unrelated first message'))
        node = self.store.hook(ITEM, 'UserPromptSubmit', 'p2', '[1] A\n\nDo A\n\n— Chat Tree ' + op['token'])
        self.assertTrue(node['adopted'])
        self.assertEqual(self.store.view(ITEM)['current_node_id'], self.a)
        self.assertEqual(self.store.job(op['id'], op['token'])['stage'], 'done')
        # A second chat with the same card cannot take the item.
        self.assertIsNone(self.store.hook(OTHER, 'UserPromptSubmit', 'p3', 'copy ' + op['token']))

    def test_reservation_repeats_until_the_chat_exists(self):
        op = self.start(self.a)
        self.assertEqual(self.store.reserve_creation(op['id'], op['token'], self.a, ROOT, repeat=True)['id'], self.a)
        self.store.hook(ITEM, 'UserPromptSubmit', 'p', op['token'])
        with self.assertRaises(TreeError):
            self.store.reserve_creation(op['id'], op['token'], self.a, ROOT, repeat=True)

    def test_only_the_parent_chat_creates_item_chats(self):
        with self.assertRaises(TreeError) as error:
            self.store.apply({'action': 'start', 'node_id': self.a}, EVIDENCE, OTHER)
        self.assertEqual(error.exception.code, 'open_parent')
        self.assertEqual(error.exception.details['url'], '#' + ROOT)

    def test_activity_reported_by_the_host_replaces_hooks(self):
        op = self.start(self.a)
        self.store.hook(ITEM, 'UserPromptSubmit', 'p', op['token'])
        self.state[ITEM] = 'working'
        with self.assertRaises(TreeError) as error:
            self.store.apply({'action': 'finish', 'node_id': self.a}, EVIDENCE, ROOT)
        self.assertEqual(error.exception.details['chats'][0]['url'], '#' + ITEM)
        # An interrupted turn leaves no hook trace; the process reports idle and nothing stays unknown.
        self.state[ITEM] = 'idle'
        self.assertEqual(self.store.apply({'action': 'finish', 'node_id': self.a}, EVIDENCE, ROOT)['operation']['kind'], 'finish')

    def test_outline_shows_the_tree_with_chat_links(self):
        op = self.start(self.a)
        self.store.hook(ITEM, 'UserPromptSubmit', 'p', op['token'])
        self.state[ITEM] = 'working'
        text = outline(self.store.view(ROOT), claude.link)
        self.assertIn('### [Root](#' + ROOT + ') · 0/2 done', text)
        self.assertIn('- [ ] [**1** A](#' + ITEM + ') · working', text)
        self.assertIn('- [ ] **2** B', text)
        item = outline(self.store.view(ITEM), claude.link)
        self.assertTrue(item.startswith('[Root](#' + ROOT + ')\n\n- [ ] [**1** A](#' + ITEM + ')'))
        self.assertIn('**this chat**', item)
        self.assertIn('No Chat Tree is linked to this chat', outline(self.store.view(OTHER), claude.link))


class ClaudeDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.desktop = Desktop(self.temp.name)
        self.desktop.session(ROOT)
        self.store = Store(Path(self.temp.name) / 'tree.sqlite3', link=claude.link, live=self.desktop.host.live, parent_creates=True)
        self.dispatcher = Dispatcher(self.store, self.desktop.host)
        self.chat = ROOT

    def tearDown(self):
        self.temp.cleanup()

    def call(self, name, **args):
        with patch.object(ClaudeDesktop, 'current_chat', lambda host: self.chat):
            result = self.dispatcher.handle('tools/call', {'name': name, 'arguments': args})
        if result.get('isError'):
            raise TreeError(result['structuredContent']['error']['code'], result['structuredContent']['error']['message'])
        return result['structuredContent']

    def started_item(self):
        tree = self.call('tree_change', change={'action': 'create', 'title': 'Root', 'items': [{'title': 'A', 'description': 'Do A'}]})
        node = tree['item_ids'][0]
        op = self.call('tree_change', change={'action': 'start', 'node_id': node})['operation']
        self.call('tree_step', operation_id=op['id'], token=op['token'], step='claim')
        prepared = self.call('tree_create_saved_chat', operation_id=op['id'], token=op['token'], node_id=node)
        return node, op, prepared

    def test_tools_have_no_panel_or_proposals(self):
        names = {t['name'] for t in self.dispatcher.handle('tools/list', {})['tools']}
        self.assertNotIn('tree_ui', names)
        self.assertNotIn('tree_propose', names)
        self.assertEqual(self.dispatcher.handle('resources/list', {}), {'resources': []})

    def test_created_tree_inherits_the_session_folder(self):
        self.call('tree_change', change={'action': 'create', 'title': 'Root', 'items': []})
        self.assertEqual(self.store.view(ROOT)['root']['cwd'], '/work')

    def test_start_returns_start_session_arguments_with_the_card_and_token(self):
        node, op, prepared = self.started_item()
        args = prepared['start_session']
        self.assertEqual((args['initiation'], args['context'], args['use_worktree'], args['title']), ('user_asked', 'fresh', False, '[1] A'))
        self.assertTrue(args['prompt'].startswith('[1] A\n\nDo A'))
        self.assertIn(op['token'], args['prompt'])
        self.assertGreaterEqual(len(args['background']), 80)
        self.assertNotIn('model', args)

    def test_started_chat_must_inherit_parent_settings(self):
        node, op, prepared = self.started_item()
        self.desktop.session(ITEM, model='claude-haiku-5-5')
        with self.assertRaises(TreeError) as error:
            self.call('tree_step', operation_id=op['id'], token=op['token'], step='started', data={'node_id': node, 'chat_id': ITEM})
        self.assertEqual(error.exception.code, 'not_inherited')
        self.assertIn('model', str(error.exception))

    def test_started_chat_is_bound_and_verified(self):
        node, op, prepared = self.started_item()
        self.desktop.session(ITEM)
        result = self.call('tree_step', operation_id=op['id'], token=op['token'], step='started', data={'node_id': node, 'chat_id': ITEM})
        self.assertEqual((result['chat_id'], result['url'], result['operation']['stage']), (ITEM, '#' + ITEM, 'done'))
        panel = self.call('tree_panel')
        self.assertIn('[**1** A](#' + ITEM + ')', panel['outline'])

    def test_delivery_goes_to_the_parent_without_a_node_id(self):
        node, op, prepared = self.started_item()
        self.desktop.session(ITEM)
        self.call('tree_step', operation_id=op['id'], token=op['token'], step='started', data={'node_id': node, 'chat_id': ITEM})
        self.chat = ITEM
        finish = self.call('tree_change', change={'action': 'finish', 'node_id': node})['operation']
        self.call('tree_step', operation_id=finish['id'], token=finish['token'], step='claim')
        with self.assertRaises(TreeError) as error:
            self.call('tree_job', operation_id=finish['id'], token=finish['token'], purpose='summary')
        self.assertEqual(error.exception.code, 'invalid_input')
        self.call('tree_summary', operation_id=finish['id'], token=finish['token'], summary='Done')
        delivery = self.call('tree_job', operation_id=finish['id'], token=finish['token'], purpose='delivery')
        self.assertEqual(delivery['chat_id'], ROOT)

    def test_summary_wait_returns_once_saved(self):
        node, op, prepared = self.started_item()
        self.desktop.session(ITEM)
        self.call('tree_step', operation_id=op['id'], token=op['token'], step='started', data={'node_id': node, 'chat_id': ITEM})
        finish = self.call('tree_change', change={'action': 'finish', 'node_id': node})['operation']
        self.call('tree_step', operation_id=finish['id'], token=finish['token'], step='claim')
        self.store.record_summary(finish['id'], finish['token'], node, 'Done', ITEM, None)
        waited = self.call('tree_job', operation_id=finish['id'], token=finish['token'], wait_for=node)
        self.assertTrue(waited['saved'])

    def test_deleted_chat_is_recorded_only_after_desktop_deleted_it(self):
        node, op, prepared = self.started_item()
        self.desktop.session(ITEM)
        self.call('tree_step', operation_id=op['id'], token=op['token'], step='started', data={'node_id': node, 'chat_id': ITEM})
        delete = self.call('tree_change', change={'action': 'delete', 'node_id': node})['operation']
        self.call('tree_step', operation_id=delete['id'], token=delete['token'], step='claim')
        with self.assertRaises(TreeError) as error:
            self.call('tree_delete_saved_chat', operation_id=delete['id'], token=delete['token'], node_id=node)
        self.assertEqual(error.exception.code, 'not_deleted')
        self.desktop.delete(ITEM)
        self.assertIn(node, self.call('tree_delete_saved_chat', operation_id=delete['id'], token=delete['token'], node_id=node)['payload']['completed'])


class ClaudeHookTests(unittest.TestCase):
    def test_hook_binds_the_new_chat_and_presents_the_item(self):
        with tempfile.TemporaryDirectory() as temp:
            desktop = Desktop(temp)
            store = Store(Path(temp) / 'tree.sqlite3', link=claude.link, parent_creates=True, live=lambda ids: {c: 'idle' for c in ids})
            tree = store.apply({'action': 'create', 'title': 'Root', 'items': [{'title': 'A'}]}, EVIDENCE, ROOT)
            op = store.apply({'action': 'start', 'node_id': tree['item_ids'][0]}, EVIDENCE, ROOT)['operation']
            store.advance(op['id'], op['token'], 'claim', actor_chat=ROOT)
            store.reserve_creation(op['id'], op['token'], tree['item_ids'][0], ROOT, repeat=True)
            hooks = json.loads((REPO / 'claude' / 'hooks.json').read_text())['hooks']
            command = hooks['UserPromptSubmit'][0]['hooks'][0]['command']
            env = {**os.environ, 'CLAUDE_PLUGIN_ROOT': str(REPO), 'CHAT_TREE_DB': str(store.path), 'CLAUDE_CONFIG_DIR': str(desktop.config)}
            env.pop('CHAT_TREE_HOST', None)
            # The hook runs in a shell started by the Claude process; any live process with the session matches.
            desktop.process(os.getpid(), 'new-cli-session', ITEM)
            event = json.dumps({'session_id': 'new-cli-session', 'prompt_id': 'p', 'hook_event_name': 'UserPromptSubmit',
                                'prompt': '[1] A\n\n— Chat Tree ' + op['token']})
            r = subprocess.run(['sh', '-c', command], input=event, text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            context = json.loads(r.stdout)['hookSpecificOutput']['additionalContext']
            self.assertIn('Chat Tree created this chat', context)
            self.assertEqual(store.view(ITEM)['current_node_id'], tree['item_ids'][0])
            r = subprocess.run(['sh', '-c', command], input=event, text=True, capture_output=True,
                               env={**env, 'CLAUDE_PLUGIN_ROOT': '/missing-plugin'}, timeout=10)
            self.assertEqual((r.returncode, r.stdout), (0, ''))


class ClaudePackagingTests(unittest.TestCase):
    def test_both_manifests_share_one_version(self):
        codex = json.loads((REPO / 'plugins' / 'chat-tree' / '.codex-plugin' / 'plugin.json').read_text())
        claude_manifest = json.loads((REPO / '.claude-plugin' / 'plugin.json').read_text())
        self.assertEqual(codex['version'], claude_manifest['version'])

    def test_claude_configuration_has_no_machine_paths(self):
        mcp = json.loads((REPO / 'claude' / 'mcp.json').read_text())['mcpServers']['chat_tree']
        self.assertEqual(mcp['args'], ['${CLAUDE_PLUGIN_ROOT}/plugins/chat-tree/server.py'])
        self.assertEqual(mcp['env'], {'CHAT_TREE_HOST': 'claude'})
        # Claude loads default component folders from the plugin root; none of them may exist there.
        for default in ('.mcp.json', 'hooks', 'skills', 'commands', 'agents'):
            self.assertFalse((REPO / default).exists(), default)


if __name__ == '__main__':
    unittest.main()
