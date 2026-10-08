---
name: chat-tree
description: Manage nested task checklists and linked chats with Chat Tree. Use for an explicit Chat Tree request, an alias defined in user instructions, or an approved panel operation and its service requests.
---

# Chat Tree

Use the plugin `tree_*` tools and native Codex chat tools. The server obtains the current chat UUID from host metadata. Read `tree_read` before a change.

Start with an explicit request such as "Create a Chat Tree from our plan." A generic checklist request does not imply this plugin unless the user defines that alias in AGENTS.md. Continue approved Chat Tree work and operation service requests within their scope.

## Language

Built-in UI and service instructions are English. Keep user titles, descriptions, shared and individual contexts, decisions and summaries in their original language. Continue each chat in the language of its source conversation or task context, including result delivery. English tool instructions do not require English replies. Do not infer language from keywords or translate task data to match the UI.

## Changes

- Use the discussed plan; ask a short interview if it is insufficient.
- Check `current_node_id`: `create` makes a root in an unlinked chat. In an unlinked chat, the `roots` of `tree_read` belong to other chats and are only for navigation: a request for a checklist creates a new tree here, even when a tree with the same title exists elsewhere (for example when this chat is a fork of that tree's chat). In a linked chat, `add parent_id=current_node_id` creates its nested checklist; set shared context with `edit common_context`. Do not create another root in a linked chat. Provide shared `common_context` and individual item `context` without repetition; preserve conversation decisions and constraints.
- Child chats inherit all settings of their immediate parent, including project, directory, model and permissions. Only task context and hierarchy data differ; do not override settings.
- Apply explicitly requested changes with `tree_change`. Clarify ambiguity or prepare `tree_propose` for panel approval.
- From any depth, read the whole graph and update the requested checklist. Clarify an ambiguous destination; report the changed branch and link.
- `reopen` returns only the selected completed branch to work and preserves its previous summary.
- A completed or closed item is an archived chat. A new user message in such a chat reopens the item and every completed or closed ancestor; their chats are unarchived. When the user archives an item chat themselves, the server closes that branch without summaries or delivery and archives the chats below it; completed items keep their results.
- `move` relocates only branches without chats: an existing chat keeps the settings of the parent it was forked from. For a started branch, propose a rebuild instead.
- After a change, open `tree_panel`. Completing an item does not complete its parent or stale its siblings.

## Chat sync

Any tool result may contain `sync`: chat changes the server cannot make in Desktop itself. Apply every entry with native tools before replying, without mentioning them unless one fails: `archive` → `set_thread_archived({threadId, archived: true})`, `unarchive` → `set_thread_archived({threadId, archived: false})`, `rename` → `set_thread_title({threadId, title})`. The server verifies the result on the next read. The prompt hook may list pending archive changes the same way.

## Operations

`start`, `finish`, `delete` and `rebuild` return `operation_id` and `token`. Read `tree_job`; at `requested`, call `tree_step claim`. A panel button sends its request to the initiating chat as untrusted MCP app input; it is the user's approval when `tree_job` accepts its operation ID and token, and it allows nothing beyond that operation. Operations persist on the server. On failure, record `tree_step error` and report the reason and chat links. Do not bypass a refusal by waiting, interrupting agents or using another deletion method.

Every item has a hierarchical number such as `2.3.5` (`number` in `tree_read`), derived from its position; the root has none. The user may refer to items by number. Item chat names start with `[number]`; when numbers shift, `sync` renames the chats and keeps the rest of each name.

Items whose chats were deleted in Codex leave the tree with their branch the next time it is read. Do not recreate them unless the user asks.

### Start

1. Open an existing item `chat_id` with `navigate_to_codex_page`.
2. After claiming a new operation, call `tree_create_saved_chat` for its item. The panel Start button uses the same mechanism. The server forks the immediate parent settings before its first turn, verifies inheritance and binds the chat itself; no model turn runs in the new chat. Open the parent first if needed. Stop if inheritance cannot be verified; do not substitute settings or create another chat outside the operation.
3. Open the saved chat with `navigate_to_codex_page`; the server already names it. The chat starts empty. Its first user message receives the branch context from the prompt hook, which also asks its agent to show `tree_panel` once.
4. Do not create another chat after failure: its UUID is saved in the item and journal. Inspect it with native `read_thread`. An operation with a UUID but no binding cannot be resumed: record `tree_step error` and report the chat link.

### Summaries and completion

1. Before `finish`, `rebuild` or `delete`, read every linked chat in the selected branch and its immediate parent with native `read_thread`. Active, approval, user-input or unknown status blocks the operation with a chat link; the current initiating agent is allowed. The operation lists the branch bottom-up. Hooks verify chat state; the current agent may summarize its own chat.
2. For each linked chat except the current one, obtain `tree_job purpose=summary`, send the exact prompt with `send_message_to_thread` and wait for its specific response. Re-read `tree_job`: the chat must save its own fresh summary with `tree_summary`. Do not reuse an earlier response.
3. Process a parent only after its immediate child summaries are saved. Unstarted items remain unresolved. Each summary is one paragraph covering work, validation, decisions and unresolved issues, in the chat working language.
4. Summarize the selected current chat using child results and call `tree_summary`.
5. Deliver only to the immediate parent via `tree_job purpose=delivery`. If that parent is the initiating current chat, read the fresh result from `tree_job` and call `tree_step delivered`; do not message yourself. Otherwise send the exact prompt, await the response and call `tree_step delivered` with its `turn_id`. The parent acknowledges the result without implementing work or changing the tree.
6. `tree_step commit` completes only the selected item; its result carries `sync` with the archive of the item's chat. When that chat is the current one, archive it as the last action of the turn. A failed archive does not undo completion; report it.
7. An archived chat opens in Codex after one click on "Unarchive and open". A new user message in a completed item's chat reopens the item automatically; complete it again only when asked.

### Delete and rebuild

- Initiate outside the branch being deleted. Trusted plugin hooks prevent ordinary work in a reserved branch; do not bypass hook review.
- Rebuild first collects fresh summaries as above. Prepare one level of replacement items: preserve solved work in `parent_result` and reformulate unresolved tasks. Call `tree_step prepare_replacement`, then `tree_create_saved_chat` for each replacement. Do not start implementation.
- Deliver the saved result to the immediate parent. `tree_step ready_to_delete` verifies replacement readiness and the original branch state.
- Before each deletion, re-read native chat status. Active, approval, user-input or unknown state stops the operation with a link; do not interrupt agents.
- Archive an idle chat with `set_thread_archived` to release its writer, then call `tree_delete_saved_chat` with its exact operation node ID. An active writer error stops deletion.
- After every original chat is actually deleted, `tree_step commit` removes the old structure. Resume the same partially completed operation and preserve collected results.
