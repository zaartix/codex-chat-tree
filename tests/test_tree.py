import json
import os
import queue
import subprocess
import textwrap
import time
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins' / 'chat-tree'))
from store import Store, TreeError
from server import Dispatcher, caller
from runtime import CodexHistory

EVIDENCE = {'source': 'test_user_action'}

class TreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'tree.sqlite3')
        r = self.change(action='create', title='Root', cwd=self.temp.name, project_id='chosen-project',
                        common_context='Shared', items=[{'title':'A','context':'Personal A'},{'title':'B'}])
        self.root, (self.a, self.b) = r['node_id'], r['item_ids']

    def tearDown(self):
        self.temp.cleanup()

    def change(self, **value):
        return self.store.apply(value, EVIDENCE, 'root-chat')

    def node(self, id):
        with self.store.connect() as db:
            return self.store.node(db, id)

    def bind(self, node, chat):
        op = self.change(action='start', node_id=node)['operation']
        self.store.advance(op['id'], op['token'], 'claim', actor_chat='root-chat')
        self.store.bind(op['id'], op['token'], chat)
        self.store.hook(chat,'Stop','initial')
        return op

    def op(self, kind, node):
        op = self.change(action=kind,node_id=node)['operation']
        self.store.advance(op['id'],op['token'],'claim',actor_chat='root-chat')
        return op

    def summary(self, op, node, chat, value='Fresh result'):
        return self.store.record_summary(op['id'],op['token'],node,value,chat,'fresh-turn')

    def step(self, op, step, data=None):
        return self.store.advance(op['id'],op['token'],step,data,'root-chat')

    def test_creation_has_no_bootstrap_turn(self):
        from server import TOOLS
        tools={t['name']:t for t in TOOLS}
        self.assertNotIn('tree_bind',tools)
        self.assertIn('openai/ui',tools['tree_panel']['_meta'])
        op=self.change(action='start',node_id=self.a)['operation']
        self.store.advance(op['id'],op['token'],'claim',actor_chat='root-chat')
        with self.assertRaises(TreeError):self.store.service_prompt(op['id'],op['token'],self.a,'bootstrap')

    def test_deleted_item_chat_removes_its_branch_only(self):
        self.bind(self.a,'a-chat')
        deep=self.change(action='add',parent_id=self.a,items=[{'title':'A1'},{'title':'A2'}])['item_ids']
        self.bind(deep[0],'a1-chat')
        before=self.node(self.root)['revision']
        removed=self.store.forget_chats(['a-chat','unknown-chat'])
        self.assertEqual([r['chat_id'] for r in removed],['a-chat']);self.assertEqual(set(removed[0]['node_ids']),{self.a,*deep})
        ids={n['id'] for n in self.store.view('root-chat')['nodes']}
        self.assertEqual(ids,{self.root,self.b})
        self.assertGreater(self.node(self.root)['revision'],before)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM runtime WHERE chat_id IN ('a-chat','a1-chat')").fetchone())
            self.assertEqual(db.execute("SELECT action FROM audit ORDER BY id DESC").fetchone()['action'],'forget_deleted_chat')
        self.assertEqual(self.store.linked_chats(),['root-chat'])

    def test_deleted_root_chat_removes_the_tree(self):
        self.bind(self.a,'a-chat')
        self.store.propose({'action':'add','parent_id':self.b,'items':[{'title':'Later'}]},'root-chat')
        self.store.forget_chats(['root-chat'])
        self.assertEqual(self.store.view('root-chat')['roots'],[])
        with self.store.connect() as db:
            for table in ('nodes','proposals','operations','summaries','results','locks'):
                self.assertIsNone(db.execute('SELECT 1 FROM '+table).fetchone(),table)

    def test_reserved_branch_is_left_to_its_operation(self):
        self.bind(self.a,'a-chat');self.op('delete',self.a)
        self.assertEqual(self.store.forget_chats(['a-chat']),[])
        self.assertEqual(self.node(self.a)['chat_id'],'a-chat')

    def test_reading_the_tree_forgets_deleted_chats_with_throttling(self):
        self.bind(self.a,'a-chat')
        asked=[]
        class History:
            def inspect(inner,chats):
                asked.append(sorted(chats));return {'a-chat':None,'root-chat':{'name':'Root','archived':False}}
        dispatcher=Dispatcher(self.store,History())
        actor={'chat_id':'root-chat','turn_id':None,'ui':True}
        nodes=dispatcher.call('tree_read',{},actor)['nodes']
        self.assertEqual({n['id'] for n in nodes},{self.root,self.b});self.assertEqual(asked,[['a-chat','root-chat']])
        dispatcher.call('tree_read',{},actor);self.assertEqual(len(asked),1)
        class Broken:
            def inspect(inner,chats):raise RuntimeError('Codex CLI not found')
        self.assertEqual(len(Dispatcher(self.store,Broken()).call('tree_read',{},actor)['nodes']),2)

    def test_items_are_numbered_by_hierarchy(self):
        deep=self.change(action='add',parent_id=self.b,items=[{'title':'B1'},{'title':'B2'}])['item_ids']
        deeper=self.change(action='add',parent_id=deep[1],items=[{'title':'B2a'}])['item_ids'][0]
        numbers={n['title']:n['number'] for n in self.store.view('root-chat')['nodes']}
        self.assertEqual(numbers,{'Root':None,'A':'1','B':'2','B1':'2.1','B2':'2.2','B2a':'2.2.1'})
        self.assertEqual(self.store.chat_title(deeper),'[2.2.1] B2a');self.assertEqual(self.store.chat_title(self.root),'Root')
        with self.store.connect() as db:self.assertEqual([c['number'] for c in self.store.context(db,deeper)],[None,'2','2.2','2.2.1'])

    def test_chat_names_follow_renumbering_and_keep_user_text(self):
        self.bind(self.a,'a-chat');self.bind(self.b,'b-chat')
        renamed={}
        class History:
            def inspect(inner,chats):
                return {'root-chat':{'name':'Plan','archived':False},'a-chat':{'name':'[1] A','archived':False},
                        'b-chat':{'name':'[2] B, my notes','archived':False}}
            def rename(inner,names):renamed.update(names)
        dispatcher=Dispatcher(self.store,History())
        actor={'chat_id':'root-chat','turn_id':None,'ui':True}
        dispatcher.call('tree_read',{},actor);self.assertEqual(renamed,{})
        with self.store.transaction() as db:db.execute('UPDATE nodes SET position=CASE id WHEN ? THEN 1 ELSE 0 END WHERE parent_id=?',(self.a,self.root))
        dispatcher.reconciled=None;dispatcher.call('tree_read',{},actor)
        self.assertEqual(renamed,{'a-chat':'[2] A','b-chat':'[1] B, my notes'})

    def test_linked_chat_can_browse_another_tree(self):
        other = self.store.apply({'action':'create','title':'Other','items':[{'title':'X'},{'title':'Y'}]}, EVIDENCE, 'other-chat')
        with self.store.transaction() as db:
            db.execute("UPDATE nodes SET state='done' WHERE id=?", (other['item_ids'][0],))
        own = self.store.view('root-chat')
        self.assertEqual(own['root']['id'], self.root);self.assertEqual(own['current_root_id'], self.root)
        browsed = self.store.view('root-chat', root_id=other['node_id'])
        self.assertEqual(browsed['root']['id'], other['node_id'])
        self.assertEqual(browsed['current_node_id'], self.root);self.assertEqual(browsed['current_root_id'], self.root)
        progress = {r['id']: (r['done'], r['items']) for r in browsed['roots']}
        self.assertEqual(progress, {self.root: (0, 2), other['node_id']: (1, 2)})

    def test_start_idempotent_and_existing_chat(self):
        first=self.change(action='start',node_id=self.a)['operation']
        again=self.change(action='start',node_id=self.a)
        self.assertTrue(again['resumed']);self.assertEqual(first['id'],again['operation']['id'])
        self.store.bind(first['id'],first['token'],'a-chat')
        self.assertEqual(self.change(action='start',node_id=self.a)['url'],'codex://threads/a-chat')

    def test_nested_items_keep_the_explicit_tree_project(self):
        deep=self.change(action='add',parent_id=self.a,items=[{'title':'Deep'}])['item_ids'][0]
        for node in [self.root,self.a,self.b,deep]:
            self.assertEqual(self.node(node)['project_id'],'chosen-project')
            self.assertEqual(self.node(node)['cwd'],self.temp.name)

    def test_completion_marks_only_selected_keeps_children_and_siblings_current(self):
        self.bind(self.a,'a-chat');self.bind(self.b,'b-chat')
        child=self.change(action='add',parent_id=self.a,items=[{'title':'Deep'}])['item_ids'][0]
        self.bind(child,'deep-chat')
        op=self.op('finish',self.a)
        with self.assertRaisesRegex(TreeError,'immediate children'):
            self.summary(op,self.a,'a-chat')
        self.summary(op,child,'deep-chat','Child result')
        self.summary(op,self.a,'a-chat')
        with self.assertRaisesRegex(TreeError,'deliver'):
            self.step(op,'commit')
        self.step(op,'delivered');self.step(op,'commit')
        self.assertEqual(self.node(self.a)['state'],'done')
        for id in [self.root,self.b,child]:
            self.assertEqual(self.node(id)['state'],'todo');self.assertFalse(self.node(id)['stale'])
        self.assertEqual(self.node(child)['chat_id'],'deep-chat')

    def test_service_prompts_preserve_original_task_languages(self):
        shared='Gemeinsamer Kontext: die Sprache des Ausgangschats beibehalten.'
        personal='日本語の文脈をそのまま保持する。'
        title='Revisar la migración'
        summary='Ο έλεγχος ολοκληρώθηκε. Η απόφαση αποθηκεύτηκε χωρίς μετάφραση.'
        self.change(action='edit',node_id=self.root,common_context=shared)
        self.change(action='edit',node_id=self.a,title=title,context=personal)
        view=self.store.view('root-chat')
        self.assertEqual(next(n for n in view['nodes'] if n['id']==self.a)['title'],title)
        with self.store.connect() as db:context=self.store.context(db,self.a)
        self.assertEqual(context[0]['common_context'],shared);self.assertEqual(context[1]['context'],personal)
        self.bind(self.a,'a-chat')
        finish=self.op('finish',self.a)
        collection=self.store.service_prompt(finish['id'],finish['token'],self.a,'summary')['prompt']
        self.assertIn('task working language',collection)
        self.summary(finish,self.a,'a-chat',summary)
        delivery=self.store.service_prompt(finish['id'],finish['token'],self.root,'delivery')['prompt']
        self.assertEqual(json.loads(delivery.split('\n')[-1])['summary'],summary)
        self.assertIn('original language',delivery)
        self.step(finish,'delivered');self.step(finish,'commit')
        self.assertEqual(self.node(self.a)['summary'],summary)

    def test_context_revision_stales_only_dependent_nested_checklists(self):
        deep=self.change(action='add',parent_id=self.a,items=[{'title':'Deep'}])['item_ids'][0]
        self.change(action='edit',node_id=self.root,common_context='New shared',context='')
        self.assertFalse(self.node(self.a)['stale']);self.assertTrue(self.node(deep)['stale'])
        chain=self.store.view(focus_id=deep)['contexts']
        self.assertEqual(chain[0]['common_context'],'New shared')
        self.assertEqual(chain[1]['context'],'Personal A')

    def test_working_chat_blocks_operation_with_link(self):
        self.bind(self.a,'a-chat');self.store.hook('a-chat','UserPromptSubmit','work','ordinary task')
        with self.assertRaises(TreeError) as raised:self.op('rebuild',self.a)
        self.assertEqual(raised.exception.code,'busy')
        self.assertEqual(raised.exception.details['chats'][0]['url'],'codex://threads/a-chat')

    def test_unknown_chat_blocks_operation(self):
        self.bind(self.a,'a-chat');self.store.hook('a-chat','Interrupt','initial')
        with self.assertRaises(TreeError) as raised:self.op('delete',self.a)
        self.assertEqual(raised.exception.code,'busy')

    def test_lock_blocks_new_work_but_allows_exact_summary_request(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a)
        service=self.store.service_prompt(op['id'],op['token'],self.a,'summary')
        for prompt in ['ordinary work',service['prompt']+' injected']:
            with self.assertRaises(TreeError):self.store.hook('a-chat','UserPromptSubmit','blocked',prompt)
        self.store.hook('a-chat','UserPromptSubmit','summary',service['prompt'])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM runtime WHERE chat_id='a-chat'").fetchone()[0],'service')

    def test_delayed_stop_cannot_clear_newer_work(self):
        self.bind(self.a,'a-chat');self.store.hook('a-chat','UserPromptSubmit','new','task')
        self.store.hook('a-chat','Stop','old')
        with self.assertRaises(TreeError):self.op('delete',self.a)

    def test_delegated_mcp_turn_is_busy_until_its_own_stop(self):
        self.bind(self.a,'a-chat')
        self.store.observe_turn('a-chat','delegated')
        self.store.hook('a-chat','Stop','initial')
        with self.assertRaises(TreeError):self.op('finish',self.a)
        self.store.hook('a-chat','Stop','delegated')
        op=self.op('finish',self.a);self.step(op,'cancel')

    def test_mcp_activity_does_not_imply_trusted_hook_or_end_service(self):
        self.store.observe_turn('root-chat','new')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT hook_seen FROM runtime WHERE chat_id='root-chat'").fetchone()[0],0)
        self.bind(self.a,'a-chat');op=self.op('finish',self.a)
        prompt=self.store.service_prompt(op['id'],op['token'],self.a,'summary')['prompt']
        self.store.hook('a-chat','UserPromptSubmit','service',prompt)
        self.store.observe_turn('a-chat','service')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM runtime WHERE chat_id='a-chat'").fetchone()[0],'service')

    def test_finished_session_preserves_idle_but_active_session_stays_unknown(self):
        self.bind(self.a,'a-chat');self.store.hook('a-chat','SessionEnd','initial')
        op=self.op('delete',self.a);self.step(op,'cancel')
        self.store.hook('a-chat','UserPromptSubmit','active','task')
        self.store.hook('a-chat','SessionEnd','active')
        with self.assertRaises(TreeError):self.op('delete',self.a)

    def test_current_branch_operation_prompt_is_allowed_by_guard(self):
        op=self.store.apply({'action':'finish','node_id':self.root},EVIDENCE,'root-chat')['operation']
        self.store.hook('root-chat','UserPromptSubmit','coordinate',op['dispatch_prompt'])
        with self.assertRaises(TreeError):self.store.hook('root-chat','UserPromptSubmit','ordinary','unrelated work')

    def test_child_completion_reserves_idle_parent_for_result_delivery(self):
        self.bind(self.a,'a-chat');self.store.hook('root-chat','Stop','root-initial')
        op=self.store.apply({'action':'finish','node_id':self.a},EVIDENCE,'a-chat')['operation']
        with self.assertRaises(TreeError):self.store.hook('root-chat','UserPromptSubmit','unrelated','work')
        self.assertEqual(self.store.view('root-chat')['nodes'][0]['locked_by'],op['id'])

    def test_stale_revision_and_cyclic_move_are_atomic(self):
        revision=self.store.view('root-chat')['revision']
        self.change(action='add',parent_id=self.a,items=[{'title':'Deep'}])
        with self.assertRaises(TreeError):self.store.apply({'action':'edit','node_id':self.a,'title':'Lost'},EVIDENCE,'root-chat',revision)
        self.assertEqual(self.node(self.a)['title'],'A')
        with self.assertRaises(TreeError):self.change(action='move',node_id=self.a,destination_id=self.a)

    def test_move_keeps_strict_parent_inheritance(self):
        deep=self.change(action='add',parent_id=self.a,items=[{'title':'Deep'}])['item_ids'][0]
        with self.store.transaction() as db:
            db.execute("UPDATE nodes SET cwd='/elsewhere',project_id='other' WHERE id=?",(self.b,))
        self.change(action='move',node_id=deep,destination_id=self.b)
        moved=self.node(deep)
        self.assertEqual((moved['parent_id'],moved['cwd'],moved['project_id']),(self.b,'/elsewhere','other'))
        self.bind(self.a,'a-chat')
        with self.assertRaises(TreeError) as refused:self.change(action='move',node_id=self.a,destination_id=self.b)
        self.assertEqual(refused.exception.code,'started_branch');self.assertEqual(refused.exception.details['chats'][0]['chat_id'],'a-chat')
        self.assertEqual(self.node(self.a)['parent_id'],self.root)

    def test_one_level_rebuild_creates_replacements_before_deleting(self):
        self.bind(self.a,'a-chat');op=self.op('rebuild',self.a)
        with self.assertRaises(TreeError):self.step(op,'prepare_replacement',{'items':[{'title':'New'}]})
        self.summary(op,self.a,'a-chat')
        with self.assertRaises(TreeError):self.step(op,'prepare_replacement',{'items':[{'title':'New','items':[{'title':'Wrong depth'}]}]})
        prepared=self.step(op,'prepare_replacement',{'items':[{'title':'New'}],'parent_result':'Saved decision'})
        new=prepared['payload']['replacement_ids'][0]
        self.assertEqual(self.node(new)['parent_id'],self.root)
        self.assertEqual(self.node(new)['project_id'],'chosen-project')
        self.assertEqual(self.node(new)['cwd'],self.temp.name)
        with self.assertRaises(TreeError):self.step(op,'ready_to_delete')
        self.store.bind(op['id'],op['token'],'new-chat',new)
        self.step(op,'delivered');self.step(op,'ready_to_delete')
        with self.assertRaises(TreeError):self.step(op,'commit')
        self.step(op,'deleted',{'node_id':self.a});self.step(op,'commit')
        self.assertEqual(self.node(new)['chat_id'],'new-chat')
        with self.assertRaises(TreeError):self.node(self.a)
        saved=self.store.job(op['id'],op['token'])
        self.assertEqual(saved['summaries'][self.a]['summary'],'Fresh result')
        self.assertEqual(saved['payload']['parent_result'],'Saved decision')
        root=self.store.view('root-chat')['nodes'][0]
        self.assertEqual(root['results'][0]['summary'],'Saved decision')

    def test_delete_partial_failure_is_resumable_after_restart(self):
        self.bind(self.a,'a-chat');op=self.op('delete',self.a)
        with self.assertRaises(TreeError):self.step(op,'commit')
        self.step(op,'error',{'message':'active writer'})
        restarted=Store(self.store.path);saved=restarted.job(op['id'],op['token'])
        self.assertEqual(saved['stage'],'deleting');self.assertEqual(saved['error'],'active writer')
        self.assertEqual(self.node(self.a)['chat_id'],'a-chat')

    def test_service_prompt_stays_exact_when_unrelated_branch_changes_revision(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a)
        first=self.store.service_prompt(op['id'],op['token'],self.a,'summary')
        self.change(action='add',parent_id=self.root,items=[{'title':'Other task'}])
        second=self.store.service_prompt(op['id'],op['token'],self.a,'summary')
        self.assertEqual(first['prompt'],second['prompt'])

    def test_ui_completion_cannot_interrupt_its_own_working_agent(self):
        self.store.hook('root-chat','UserPromptSubmit','work','implementation')
        with self.assertRaises(TreeError):self.store.apply({'action':'finish','node_id':self.root},{'source':'user_interface'},'root-chat')

    def test_creation_reserved_once_uuid_saved_before_binding(self):
        op=self.op('start',self.a)
        self.store.reserve_creation(op['id'],op['token'],self.a,'root-chat')
        self.store.created(op['id'],op['token'],self.a,'reserved-chat')
        with self.assertRaises(TreeError):self.store.reserve_creation(op['id'],op['token'],self.a,'root-chat')
        self.assertEqual(Store(self.store.path).view('root-chat')['nodes'][1]['chat_id'],'reserved-chat')
        with self.assertRaises(TreeError):self.step(op,'cancel')

    def test_summary_cannot_be_replaced_with_another_turn(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a);self.summary(op,self.a,'a-chat')
        with self.assertRaises(TreeError):self.store.record_summary(op['id'],op['token'],self.a,'Other','a-chat','other')

    def test_root_delete_and_rebuild_denied(self):
        for kind in ['delete','rebuild']:
            with self.assertRaises(TreeError):self.op(kind,self.root)

    def test_new_turn_in_completed_item_reopens_it(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a);self.summary(op,self.a,'a-chat')
        self.step(op,'delivered');self.step(op,'commit')
        self.assertEqual(self.node(self.a)['state'],'done')
        result=self.store.hook('a-chat','UserPromptSubmit','t2','One more fix')
        self.assertTrue(result['reopened']);self.assertEqual(self.node(self.a)['state'],'todo')
        self.assertEqual(self.node(self.a)['summary'],'Fresh result')
        self.assertFalse(self.store.hook('a-chat','UserPromptSubmit','t3','Again')['reopened'])

    def test_result_delivery_to_completed_parent_keeps_it_done(self):
        self.bind(self.a,'a-chat')
        deep=self.change(action='add',parent_id=self.a,items=[{'title':'A1'}])['item_ids'][0]
        self.bind(deep,'a1-chat')
        with self.store.transaction() as db:db.execute("UPDATE nodes SET state='done' WHERE id=?",(self.a,))
        op=self.op('finish',deep);self.summary(op,deep,'a1-chat')
        delivery=self.store.service_prompt(op['id'],op['token'],self.a,'delivery')['prompt']
        self.assertFalse(self.store.hook('a-chat','UserPromptSubmit','d1',delivery)['reopened'])
        self.assertEqual(self.node(self.a)['state'],'done')

    def test_explicit_reopen_preserves_previous_result_and_parent_state(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a);self.summary(op,self.a,'a-chat')
        self.step(op,'delivered');self.step(op,'commit')
        self.change(action='reopen',node_id=self.a)
        self.assertEqual(self.node(self.a)['state'],'todo')
        self.assertEqual(self.node(self.a)['summary'],'Fresh result')
        self.assertEqual(self.node(self.root)['state'],'todo')

    def test_service_validation_consumes_only_previously_issued_exact_requests(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a)
        prompt=self.store.service_prompt(op['id'],op['token'],self.a,'summary')['prompt']
        self.store.check_service(op['id'],op['token'],self.a,prompt,'summary')
        for node,value,purpose in [(self.a,prompt+' extra','summary'),(self.b,prompt,'summary'),(self.a,prompt,'bootstrap')]:
            with self.assertRaises(TreeError):self.store.check_service(op['id'],op['token'],node,value,purpose)
        self.step(op,'cancel')
        with self.assertRaises(TreeError):self.store.check_service(op['id'],op['token'],self.a,prompt,'summary')

    def test_hook_executable_injects_context_and_blocks_locked_work(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a)
        guard=Path(__file__).resolve().parents[1]/'plugins'/'chat-tree'/'hooks'/'guard.py'
        env={**os.environ,'CHAT_TREE_DB':str(self.store.path)}
        def invoke(prompt):
            r=subprocess.run([sys.executable,str(guard)],input=json.dumps({'session_id':'a-chat','turn_id':'guard-turn','hook_event_name':'UserPromptSubmit','prompt':prompt}),text=True,capture_output=True,env=env,timeout=5,check=True)
            return json.loads(r.stdout)
        self.assertEqual(invoke('ordinary work')['decision'],'block')
        prompt=self.store.service_prompt(op['id'],op['token'],self.a,'summary')['prompt']
        result=invoke(prompt)['hookSpecificOutput']
        self.assertEqual(result['hookEventName'],'UserPromptSubmit')
        self.assertIn('Personal A',result['additionalContext']);self.assertIn('Shared',result['additionalContext'])

    def test_internal_subagent_hook_cannot_release_main_chat(self):
        self.bind(self.a,'a-chat');self.store.observe_turn('a-chat','main')
        guard=Path(__file__).resolve().parents[1]/'plugins'/'chat-tree'/'hooks'/'guard.py'
        subprocess.run([sys.executable,str(guard)],input=json.dumps({'session_id':'a-chat','agent_id':'internal-child','turn_id':'main','hook_event_name':'Stop'}),text=True,capture_output=True,env={**os.environ,'CHAT_TREE_DB':str(self.store.path)},timeout=5,check=True)
        self.assertEqual(self.store.view('a-chat')['nodes'][1]['runtime']['state'],'running')

    def test_cancelled_operation_cannot_dispatch_new_summary_requests(self):
        self.bind(self.a,'a-chat');op=self.op('finish',self.a);self.step(op,'cancel')
        with self.assertRaises(TreeError):self.store.service_prompt(op['id'],op['token'],self.a,'summary')

    def test_change_requires_audit_source(self):
        with self.assertRaises(TreeError):self.store.apply({'action':'edit','node_id':self.a,'title':'No'},None)
        self.assertEqual(self.node(self.a)['title'],'A')

    def test_approved_existing_chat_proposal_is_consumed(self):
        self.bind(self.a,'a-chat');p=self.store.propose({'action':'start','node_id':self.a})
        self.store.apply({},EVIDENCE,'root-chat',proposal_id=p['proposal_id'])
        self.assertFalse(self.store.view('root-chat')['proposals'])

class ProtocolTests(unittest.TestCase):
    def test_create_inherits_source_project_and_rejects_overrides(self):
        class History:
            def __init__(self,cwd,project):self.cwd=cwd;self.project=project;self.reads=[]
            def request(self,method,params):
                self.reads.append((method,params))
                return {'thread':{'cwd':self.cwd,'projectId':self.project}}
        for name in ['tree_change','tree_propose','tree_ui']:
            for explicit_cwd in [False,True]:
                for source in [None,'source-project']:
                    for project in ['omitted',None,'source-project','other-project']:
                        with self.subTest(tool=name,cwd=explicit_cwd,source=source,project=project), tempfile.TemporaryDirectory() as tmp:
                            history=History(tmp,source);store=Store(Path(tmp)/'db');d=Dispatcher(store,history)
                            change={'action':'create','title':'Root','items':[{'title':'Child'}]}
                            if explicit_cwd:change['cwd']=tmp
                            if project!='omitted':change['project_id']=project
                            args={'change':change};actor={'chat_id':'chat','turn_id':'turn','ui':name=='tree_ui'}
                            if project!='omitted' and project!=source:
                                with self.assertRaises(TreeError) as raised:d.call(name,args,actor)
                                self.assertEqual(raised.exception.code,'invalid_input')
                                self.assertFalse(store.view('chat')['roots']);self.assertFalse(store.view('chat')['proposals'])
                                continue
                            result=d.call(name,args,actor)
                            if name=='tree_propose':
                                self.assertEqual(result['change']['project_id'],source)
                                d.call('tree_ui',{'proposal_id':result['proposal_id']},{'chat_id':'chat','turn_id':None,'ui':True})
                            for node in store.view('chat')['nodes']:
                                self.assertEqual(node['project_id'],source)
                                self.assertEqual(node['cwd'],tmp)
                            self.assertEqual(len(history.reads),1)
                            if not explicit_cwd:self.assertNotIn('cwd',change)

    def test_create_rejects_workspace_override(self):
        class History:
            def request(self,*args):return {'thread':{'cwd':'/parent','projectId':None}}
        for name in ['tree_change','tree_propose','tree_ui']:
            with self.subTest(tool=name),tempfile.TemporaryDirectory() as tmp:
                d=Dispatcher(Store(Path(tmp)/'db'),History())
                args={'change':{'action':'create','title':'Root','cwd':'/other'}}
                with self.assertRaisesRegex(TreeError,'cwd'):
                    d.call(name,args,{'chat_id':'chat','turn_id':'turn','ui':name=='tree_ui'})
                self.assertFalse(d.store.view('chat')['roots'])

    def test_runtime_reports_timeout_and_does_not_auto_confirm_host_requests(self):
        events=queue.Queue()
        with self.assertRaisesRegex(RuntimeError,'timed out'):
            CodexHistory._next_event(events,time.monotonic(),'bootstrap')
        events.put({'jsonrpc':'2.0','id':9,'method':'item/tool/requestApproval'})
        with self.assertRaisesRegex(RuntimeError,'interactive confirmation'):
            CodexHistory._next_event(events,time.monotonic()+1,'bootstrap')

    def test_appserver_keeps_settings_notification_received_before_rpc_reply(self):
        script=textwrap.dedent('''\
            #!/usr/bin/env python3
            import json,sys
            for line in sys.stdin:
                request=json.loads(line)
                if 'id' not in request:continue
                if request['method']=='probe/settings':
                    print(json.dumps({'method':'thread/settings/updated','params':{'threadId':'child'}}),flush=True)
                print(json.dumps({'id':request['id'],'result':{}}),flush=True)
        ''')
        with tempfile.TemporaryDirectory() as tmp:
            executable=Path(tmp)/'codex';executable.write_text(script);executable.chmod(0o700)
            with CodexHistory(str(executable)).connection() as (call,events):
                call(2,'probe/settings',{})
                event=CodexHistory._next_event(events,time.monotonic()+1,'settings test')
                self.assertEqual(event['method'],'thread/settings/updated')
                self.assertEqual(event['params']['threadId'],'child')

    def test_host_metadata_distinguishes_ui_and_model(self):
        self.assertTrue(caller({'_meta':{'threadId':'id'}})['ui'])
        m=caller({'_meta':{'threadId':'id','x-codex-turn-metadata':json.dumps({'thread_id':'id','turn_id':'turn'})}})
        self.assertFalse(m['ui']);self.assertEqual(m['turn_id'],'turn')

    def test_native_delegation_framing_keeps_input_opaque(self):
        prompt='text <input>nested</input>\nother'
        value='<codex_delegation>\n  <source_thread_id>root</source_thread_id>\n  <input>'+prompt+'</input>\n</codex_delegation>'
        self.assertEqual(CodexHistory.delegation(value)['quote'],prompt)
        self.assertIsNone(CodexHistory.delegation('fake '+value))

    def test_agent_cannot_call_ui_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            d=Dispatcher(Store(Path(tmp)/'db'),object())
            with self.assertRaises(TreeError):d.call('tree_ui',{'change':{'action':'create','title':'Root','cwd':tmp}}, {'chat_id':'chat','turn_id':'turn','ui':False})

    def test_agent_change_requires_native_chat_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher=Dispatcher(Store(Path(tmp)/'db'),object())
            with self.assertRaises(TreeError) as raised:
                dispatcher.call('tree_change',{'change':{'action':'create','title':'Root'}},
                                {'chat_id':None,'turn_id':None,'ui':False})
            self.assertEqual(raised.exception.code,'no_chat')
            self.assertFalse(dispatcher.store.view()['roots'])

    def test_server_records_model_activity_even_without_prompt_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db')
            store.apply({'action':'create','title':'Root','cwd':tmp},EVIDENCE,'chat')
            d=Dispatcher(store,object())
            result=d.handle('tools/call',{'name':'tree_read','_meta':{'x-codex-turn-metadata':{'thread_id':'chat','turn_id':'delegated'}}})
            self.assertFalse(result.get('isError',False))
            self.assertEqual(store.view('chat')['nodes'][0]['runtime']['state'],'running')
            store.hook('chat','Stop','delegated')
            self.assertEqual(store.view('chat')['nodes'][0]['runtime']['state'],'idle')

    def test_stdio_server_answers_ping_while_another_request_is_pending(self):
        plugin=Path(__file__).resolve().parents[1]/'plugins'/'chat-tree'
        script=textwrap.dedent('''
            import sys,time
            sys.path.insert(0,sys.argv[1])
            import server
            original=server.Dispatcher.handle
            def delayed(self,method,params):
                if method=='test/pending':
                    time.sleep(.4)
                    return {}
                return original(self,method,params)
            server.Dispatcher.handle=delayed
            server.main()
        ''')
        with tempfile.TemporaryDirectory() as tmp:
            r=subprocess.run([sys.executable,'-c',script,str(plugin)],input='\n'.join(json.dumps({'jsonrpc':'2.0','id':i,'method':m}) for i,m in [(1,'test/pending'),(2,'ping')])+'\n',text=True,capture_output=True,env={**os.environ,'CHAT_TREE_DB':str(Path(tmp)/'db')},timeout=5,check=True)
            replies=[json.loads(line) for line in r.stdout.splitlines()]
            self.assertEqual([v['id'] for v in replies],[2,1])
            self.assertTrue(all('result' in v for v in replies))

    def test_agent_changes_use_host_context_without_reading_user_history(self):
        class History:
            def __init__(self,cwd):self.cwd=cwd;self.calls=[]
            def request(self,method,params):
                self.calls.append((method,params))
                if method!='thread/read' or params.get('includeTurns') is not False:
                    raise AssertionError('An ordinary change must not inspect user messages')
                return {'thread':{'cwd':self.cwd,'projectId':'parent-project'}}
        for turn_id in [None,'current']:
            with self.subTest(turn_id=turn_id),tempfile.TemporaryDirectory() as tmp:
                history=History(tmp);store=Store(Path(tmp)/'db');dispatcher=Dispatcher(store,history)
                metadata={'thread_id':'chat'}
                if turn_id:metadata['turn_id']=turn_id
                def change(value):
                    result=dispatcher.handle('tools/call',{'name':'tree_change','arguments':{'change':value},
                        '_meta':{'x-codex-turn-metadata':metadata}})
                    self.assertFalse(result.get('isError'),result)
                    return result['structuredContent']
                root=change({'action':'create','title':'Root','items':[{'title':'Audit'}]})['node_id']
                change({'action':'add','parent_id':root,'items':[{'title':'Implement'}]})
                change({'action':'edit','node_id':root,'common_context':'Approved plan'})
                self.assertEqual(len(store.view('chat')['nodes']),3)
                self.assertEqual(len(history.calls),1)
                with store.connect() as db:
                    entries=[json.loads(row[0]) for row in db.execute('SELECT evidence FROM audit')]
                    self.assertEqual(entries,[{'source':'agent_tool','chat_id':'chat','turn_id':turn_id}]*3)

    def test_panel_cannot_use_model_change_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatcher=Dispatcher(Store(Path(tmp)/'db'),object())
            with self.assertRaises(TreeError) as raised:
                dispatcher.call('tree_change',{'change':{'action':'create','title':'Root'}},
                                {'chat_id':'chat','turn_id':None,'ui':True})
            self.assertEqual(raised.exception.code,'denied')
            self.assertFalse(dispatcher.store.view()['roots'])

    def test_service_prompt_still_reads_exact_current_turn_text(self):
        history=CodexHistory()
        prompt='\nExact service prompt\n'
        delegated='<codex_delegation>\n  <source_thread_id>parent</source_thread_id>\n  <input>'+prompt+'</input>\n</codex_delegation>'
        for item in [
            {'type':'userMessage','content':[{'type':'text','text':prompt}]},
            {'type':'functionCallOutput','namespace':'codex_app','name':'send_message_to_thread','output':delegated}]:
            with self.subTest(type=item['type']),patch.object(history,'request',return_value={'thread':{'turns':[
                {'id':'old','items':[{'type':'userMessage','content':[{'type':'text','text':'old'}]}]},
                {'id':'current','items':[item]}]}}):
                self.assertEqual(history.user_prompt('chat','current'),prompt)
                with self.assertRaises(RuntimeError):history.user_prompt('chat','missing')


class InheritanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.cwd=self.temp.name
        self.settings={'cwd':self.cwd,'model':'parent-model','model_provider_id':'parent-provider',
                       'reasoning_effort':'low','reasoning_summary':'detailed',
                       'runtime_workspace_roots':[self.cwd,'/another-root'],
                       'approval_policy':'on-request','approvals_reviewer':'user',
                       'permission_profile':{'type':'disabled'},'active_permission_profile':{'id':':danger-full-access'},
                       'collaboration_mode':{'mode':'plan','settings':{'model':'parent-model','reasoning_effort':'low','developer_instructions':'Parent rules'}},
                       'personality':'none','disabled_plugin_ids':['disabled-plugin'],'service_tier':'default'}
        self.instructions={'text':'Source system instructions'}
        self.parent=self.thread('parent-chat');self.child=self.thread('new-chat')
        self.write(self.parent,self.settings)
        self.calls=[];self.saved=[];self.reserved=[];self.events=queue.Queue()
        self.events.put({'method':'thread/settings/updated','params':{'threadId':'new-chat'}})
        self.events.put({'method':'turn/completed','params':{'threadId':'parent-chat','turn':{'id':'unrelated','status':'failed'}}})
        self.events.put({'method':'turn/completed','params':{'threadId':'new-chat','turn':{'id':'bootstrap-turn','status':'completed'}}})
        self.history=CodexHistory()

    def tearDown(self):self.temp.cleanup()

    def thread(self,id):
        return {'id':id,'cwd':self.cwd,'projectId':'parent-project','path':str(Path(self.cwd)/(id+'.jsonl')),'turns':[]}

    def write(self,thread,settings,instructions=None,extra=None):
        records=[{'type':'session_meta','payload':{'id':thread['id'],'base_instructions':self.instructions if instructions is None else instructions}},
                 {'type':'event_msg','payload':{'type':'thread_settings_applied','thread_settings':settings}}]
        if extra:records.extend(extra)
        Path(thread['path']).write_text('\n'.join(json.dumps(r) for r in records)+'\n')

    def run_create(self,mutation=None):
        self.events=queue.Queue()
        self.events.put({'method':'thread/settings/updated','params':{'threadId':'new-chat'}})
        self.events.put({'method':'turn/completed','params':{'threadId':'parent-chat','turn':{'id':'unrelated','status':'failed'}}})
        self.events.put({'method':'turn/completed','params':{'threadId':'new-chat','turn':{'id':'bootstrap-turn','status':'completed'}}})
        def call(identifier,method,params):
            self.calls.append((method,params))
            if method=='thread/read':
                if params['threadId']=='parent-chat':return {'thread':self.parent}
                return {'thread':self.child}
            if method=='thread/turns/list':return {'data':[{'id':'first-parent-turn'}]}
            if method=='thread/fork':
                self.write(self.child,self.settings)
                if mutation:mutation()
                return {'thread':self.child}
            return {}
        @contextmanager
        def connection():yield call,self.events
        with patch.object(self.history,'connection',connection):
            return self.history.create('parent-chat',self.saved.append,title='Child title',reserve=lambda:self.reserved.append(True))

    def test_only_missing_chats_count_as_deleted(self):
        def call(identifier,method,params):
            if params['threadId']=='gone':raise RuntimeError('thread not loaded: gone')
            if params['threadId']=='flaky':raise RuntimeError('Codex AppServer timed out during thread/read')
            path='/x/archived_sessions/r.jsonl' if params['threadId']=='old' else '/x/sessions/r.jsonl'
            return {'thread':{'id':params['threadId'],'name':'[1] A','path':path}}
        @contextmanager
        def connection():yield call,self.events
        with patch.object(self.history,'connection',connection):
            self.assertEqual(self.history.inspect(['alive','gone','flaky','old']),
                             {'alive':{'name':'[1] A','archived':False},'gone':None,'old':{'name':'[1] A','archived':True}})
        self.assertEqual(self.history.inspect([]),{})

    def test_fork_inherits_settings_without_parent_history(self):
        result=self.run_create()
        fork=next(params for method,params in self.calls if method=='thread/fork')
        self.assertEqual(fork,{'threadId':'parent-chat','beforeTurnId':'first-parent-turn','excludeTurns':True,
                              'ephemeral':False,'model':'parent-model','modelProvider':'parent-provider',
                              'cwd':self.cwd,'runtimeWorkspaceRoots':[self.cwd,'/another-root'],
                              'approvalPolicy':'on-request','approvalsReviewer':'user','serviceTier':'default',
                              'permissions':':danger-full-access','config':{'model_reasoning_effort':'low'}})
        update=next(params for method,params in self.calls if method=='thread/settings/update')
        self.assertEqual(update['collaborationMode'],self.settings['collaboration_mode'])
        self.assertEqual(update['disabledPluginIds'],['disabled-plugin'])
        self.assertEqual(update['summary'],'detailed')
        self.assertFalse(any(method=='thread/start' for method,params in self.calls))
        self.assertEqual(self.saved,['new-chat']);self.assertEqual(result['turn_id'],None)
        self.assertEqual(self.reserved,[True])

    def test_fork_without_prompt_runs_no_model_turn(self):
        result=self.run_create()
        self.assertEqual(result,{'chat_id':'new-chat','turn_id':None});self.assertEqual(self.saved,['new-chat'])
        self.assertFalse(any(method=='turn/start' for method,params in self.calls))
        self.assertIn('thread/settings/update',[method for method,params in self.calls])

    def test_hooks_trusted_requires_every_enabled_trusted_lifecycle_hook(self):
        def listing(*hooks):return {'data':[{'cwd':'/x','hooks':[{'pluginId':'chat-tree@local','eventName':e,'enabled':en,'trustStatus':t} for e,en,t in hooks]}]}
        events=['userPromptSubmit','stop','interrupt','sessionEnd']
        for hooks,expected in [([(e,True,'trusted') for e in events],True),
                               ([(e,True,'trusted') for e in events[:3]],False),
                               ([(e,True,'modified' if e=='stop' else 'trusted') for e in events],False),
                               ([(e,e!='interrupt','trusted') for e in events],False)]:
            with self.subTest(hooks=hooks),patch.object(self.history,'request',return_value=listing(*hooks)):
                self.assertEqual(self.history.hooks_trusted(),expected)

    def test_incomplete_parent_snapshot_is_an_error_before_creation(self):
        for missing in ['runtime_workspace_roots','active_permission_profile','service_tier','model']:
            with self.subTest(missing=missing):
                settings={k:v for k,v in self.settings.items() if k!=missing}
                self.write(self.parent,settings);self.calls.clear()
                with self.assertRaisesRegex(RuntimeError,'complete parent settings'):self.run_create()
                self.assertFalse(any(method=='thread/fork' for method,params in self.calls))
                self.assertFalse(self.saved)
                self.assertFalse(self.reserved)

    def test_latest_turn_settings_override_the_previous_snapshot(self):
        self.write(self.parent,self.settings,extra=[{'type':'turn_context','payload':{
            'workspace_roots':['/latest-root'],'effort':'high','disabled_plugin_ids':[]}}])
        settings,instructions=CodexHistory._settings(self.parent)
        self.assertEqual(settings['runtime_workspace_roots'],['/latest-root'])
        self.assertEqual(settings['reasoning_effort'],'high')
        self.assertEqual(settings['disabled_plugin_ids'],[])
        self.assertEqual(settings['service_tier'],'default');self.assertEqual(instructions,self.instructions)

    def test_mismatch_preserves_uuid_and_never_starts_model_work(self):
        for field,value in [('model','wrong-model'),('runtime_workspace_roots',[self.cwd]),
                            ('permission_profile',{'type':'restricted'}),('service_tier','wrong-tier')]:
            with self.subTest(field=field):
                self.calls.clear();self.saved.clear()
                def mutation():self.write(self.child,{**self.settings,field:value})
                with self.assertRaisesRegex(RuntimeError,field):self.run_create(mutation)
                self.assertEqual(self.saved,['new-chat'])
                self.assertFalse(any(method=='turn/start' for method,params in self.calls))

    def test_history_project_and_instructions_cannot_change(self):
        for field in ['projectId','parent_history','base_instructions']:
            with self.subTest(field=field):
                self.calls.clear();self.saved.clear();self.child=self.thread('new-chat')
                def mutation():
                    if field=='projectId':self.child['projectId']='other-project'
                    elif field=='parent_history':self.child['turns']=[{'id':'old-parent-turn'}]
                    else:self.write(self.child,self.settings,{'text':'Changed system instructions'})
                with self.assertRaisesRegex(RuntimeError,field):self.run_create(mutation)
                self.assertEqual(self.saved,['new-chat'])
                self.assertFalse(any(method=='turn/start' for method,params in self.calls))

    def test_parent_change_during_fork_is_an_error(self):
        def mutation():self.write(self.parent,{**self.settings,'model':'new-parent-model'})
        with self.assertRaisesRegex(RuntimeError,'changed during creation'):self.run_create(mutation)
        self.assertEqual(self.saved,['new-chat'])
        self.assertFalse(any(method=='turn/start' for method,params in self.calls))

    def test_dispatcher_uses_immediate_parent_chat(self):
        store=Store(Path(self.cwd)/'tree.db')
        root=store.apply({'action':'create','title':'Root','cwd':self.cwd,'items':[{'title':'A'}]},EVIDENCE,'root-chat')
        a=root['item_ids'][0]
        op=store.apply({'action':'start','node_id':a},EVIDENCE,'root-chat')['operation']
        store.bind(op['id'],op['token'],'a-chat')
        deep=store.apply({'action':'add','parent_id':a,'items':[{'title':'Deep'}]},EVIDENCE,'root-chat')['item_ids'][0]
        op=store.apply({'action':'start','node_id':deep},EVIDENCE,'root-chat')['operation']
        store.advance(op['id'],op['token'],'claim',actor_chat='root-chat')
        class History:
            def create(inner,parent_chat_id,saved,title=None,reserve=None):
                self.assertEqual(parent_chat_id,'a-chat')
                reserve()
                saved('deep-chat')
                return {'chat_id':'deep-chat','turn_id':None}
            def hooks_trusted(inner):return True
        result=Dispatcher(store,History()).call('tree_create_saved_chat',{'operation_id':op['id'],'token':op['token'],'node_id':deep},{'chat_id':'root-chat','turn_id':'current','ui':False})
        self.assertEqual(result['chat_id'],'deep-chat')
        view=store.view('deep-chat')
        self.assertEqual(view['current_node_id'],deep);self.assertFalse(view['operations'])
        self.assertEqual({k:v for k,v in next(n for n in view['nodes'] if n['id']==deep)['runtime'].items() if k!='updated'},{'chat_id':'deep-chat','state':'idle','hook_seen':1})
        # The first user prompt is flagged so the hook asks the agent to show the panel; later prompts are not.
        self.assertTrue(store.hook('deep-chat','UserPromptSubmit','first','Start work')['first_prompt'])
        store.hook('deep-chat','Stop','first')
        self.assertFalse(store.hook('deep-chat','UserPromptSubmit','second','Continue')['first_prompt'])

    def test_untrusted_hooks_leave_new_chat_state_unknown(self):
        store=Store(Path(self.cwd)/'tree.db')
        root=store.apply({'action':'create','title':'Root','cwd':self.cwd,'items':[{'title':'A'}]},EVIDENCE,'root-chat')
        op=store.apply({'action':'start','node_id':root['item_ids'][0]},EVIDENCE,'root-chat')['operation']
        class History:
            def create(inner,parent_chat_id,saved,title=None,reserve=None):
                reserve();saved('a-chat');return {'chat_id':'a-chat','turn_id':None}
            def hooks_trusted(inner):return False
        Dispatcher(store,History()).call('tree_create_saved_chat',{'operation_id':op['id'],'token':op['token'],'node_id':root['item_ids'][0]},{'chat_id':'root-chat','turn_id':None,'ui':True})
        node=next(n for n in store.view('root-chat')['nodes'] if n['chat_id']=='a-chat')
        self.assertEqual(node['runtime']['state'],'unknown')

    def test_unopened_parent_cannot_be_used_for_inheritance(self):
        store=Store(Path(self.cwd)/'tree.db')
        root=store.apply({'action':'create','title':'Root','cwd':self.cwd,'items':[{'title':'A'}]},EVIDENCE,'root-chat')
        a=root['item_ids'][0]
        deep=store.apply({'action':'add','parent_id':a,'items':[{'title':'Deep'}]},EVIDENCE,'root-chat')['item_ids'][0]
        with self.assertRaises(TreeError) as raised:store.apply({'action':'start','node_id':deep},EVIDENCE,'root-chat')
        self.assertEqual(raised.exception.code,'parent_not_started')
        self.assertEqual(raised.exception.details['node_id'],a)
        self.assertFalse(store.view('root-chat')['operations'])

class ReleaseTests(unittest.TestCase):
    def test_incompatible_database_is_reported_not_migrated(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            for version,table in ((7,False),(0,True)):
                path=Path(temp)/('v'+str(version)+'.sqlite3')
                with sqlite3.connect(path) as db:
                    db.execute('PRAGMA user_version='+str(version))
                    if table:db.execute('CREATE TABLE nodes(id TEXT)')
                with self.assertRaises(TreeError) as raised:Store(path)
                self.assertEqual(raised.exception.code,'schema')
                with patch('server.Store',side_effect=raised.exception):
                    result=Dispatcher(history=object()).handle('tools/call',{'name':'tree_read','arguments':{}})
                self.assertTrue(result['isError']);self.assertEqual(result['structuredContent']['error']['code'],'schema')
            Store(Path(temp)/'fresh.sqlite3');Store(Path(temp)/'fresh.sqlite3')

    def test_release_version_names_the_panel_resource(self):
        import server
        version=json.loads((server.ROOT/'.codex-plugin'/'plugin.json').read_text())['version']
        self.assertEqual(server.UI_URI,'ui://chat-tree/v'+version+'/tree.html')
        with tempfile.TemporaryDirectory() as temp:
            html=Dispatcher(Store(Path(temp)/'t.sqlite3'),object()).handle('resources/read',{'uri':server.UI_URI})['contents'][0]['text']
        self.assertIn("version:'"+version+"'",html);self.assertNotIn('__VERSION__',html)

    def test_bundled_configuration_has_no_machine_paths(self):
        plugin=Path(__file__).resolve().parents[1]/'plugins'/'chat-tree'
        mcp=json.loads((plugin/'.mcp.json').read_text())['mcpServers']['chat_tree']
        self.assertEqual((mcp['command'],mcp['args'],mcp['cwd']),('python3',['./server.py'],'.'))
        hooks=json.loads((plugin/'hooks'/'hooks.json').read_text())['hooks']
        commands={h['command'] for groups in hooks.values() for g in groups for h in g['hooks']}
        self.assertEqual(commands,{'python3 "${PLUGIN_ROOT}/hooks/guard.py"'})


if __name__=='__main__':unittest.main()
