---
name: chat-tree
description: Manage nested task checklists where every item is its own Claude Code session, with Chat Tree. Use for an explicit Chat Tree request, an alias defined in user instructions, or an approved Chat Tree operation and its service requests.
---

# Chat Tree

Use the plugin `tree_*` tools and the native Claude Desktop session tools (`start_session`, `SendMessage` or `send_message`, `archive_session`, `unarchive_session`, `set_session_title`, `delete_session`, `open_session_in`). Chats are Desktop sessions named by their `local_…` ID; the server knows the current one. Read `tree_read` before a change.

Start with an explicit request such as "Create a Chat Tree from our plan." A generic checklist request does not imply this plugin unless the user defines that alias in CLAUDE.md. Continue approved Chat Tree work and operation service requests within their scope.

## Showing the tree

There is no panel: `tree_panel` returns `outline`, a Markdown checklist whose links open the item chats. Show it exactly as returned, without a code block, after every change and whenever the user asks for the tree. In the root chat it shows the whole tree; in an item chat it shows the parent path, the item and its nested checklist. Mention item chats as `[title](#local_…)` links.

## Language

Built-in texts and service instructions are English. Keep user titles, descriptions, shared and individual contexts, decisions and summaries in their original language. Continue each chat in the language of its source conversation or task context, including result delivery. English tool instructions do not require English replies. Do not infer language from keywords or translate task data to match the built-in texts.

## Changes

- Use the discussed plan; ask a short interview if it is insufficient.
- Check `current_node_id`: `create` makes a root in an unlinked chat. In an unlinked chat, the `roots` of `tree_read` belong to other chats and are only for navigation: a request for a checklist creates a new tree here, even when a tree with the same title exists elsewhere. In a linked chat, `add parent_id=current_node_id` creates its nested checklist; set shared context with `edit common_context`. Do not create another root in a linked chat. Provide shared `common_context` and individual item `context` without repetition; preserve conversation decisions and constraints.
- Item chats inherit the settings of their immediate parent: folder, model, effort and permission mode. Only task context and hierarchy data differ; do not override settings.
- Apply explicitly requested changes with `tree_change`. Clarify ambiguity first; there is no proposal step.
- From any depth, read the whole graph and update the requested checklist. Clarify an ambiguous destination; report the changed branch and link.
- `reopen` returns only the selected completed branch to work and preserves its previous summary.
- A completed or closed item is an archived chat. A new user message in such a chat reopens the item and every completed or closed ancestor; their chats are unarchived. When the user archives an item chat themselves, the server closes that branch without summaries or delivery and archives the chats below it; completed items keep their results. Desktop nests item chats under the chat that started them and archives idle nested chats with their parent; the server treats that as the same closing.
- `move` relocates only branches without chats: an existing chat keeps the settings of the parent it was created from. For a started branch, propose a rebuild instead.
- Completing an item does not complete its parent or stale its siblings.

## Chat sync

Any tool result may contain `sync`: chat changes only you can make in Desktop. Apply every entry with native tools before replying, without mentioning them unless one fails: `archive` → `archive_session({session_id: chat_id})`, `unarchive` → `unarchive_session({session_id: chat_id})`, `rename` → `set_session_title({session_id: chat_id, title})`. The server verifies the result on the next read. The prompt hook may list pending archive changes the same way. An archive that Desktop refuses because the chat is working or open on screen stays pending; mention it once.

## Operations

`start`, `finish`, `delete` and `rebuild` return `operation_id` and `token`. Read `tree_job`; at `requested`, call `tree_step claim`. The user's request in this chat is the approval of exactly that operation. Operations persist on the server. On failure, record `tree_step error` and report the reason and chat links. Do not bypass a refusal by waiting, stopping sessions or using another deletion method.

Every item has a hierarchical number such as `2.3.5` (`number` in `tree_read`), derived from its position; the root has none. The user may refer to items by number. Item chat names start with `[number]`; when numbers shift, `sync` renames the chats and keeps the rest of each name.

Items whose chats were deleted in Desktop leave the tree with their branch the next time it is read. Do not recreate them unless the user asks.

A chat counts as busy while its Claude process is working; the server reads that itself. Busy chats in a branch block completion, rebuilding and deletion, with links.

### Start

1. An item that already has a chat: give its link. Open it with `open_session_in` only when the user asks to go there now.
2. A new item chat is created only from the chat of its immediate parent (the root chat for top-level items), because a new session inherits the settings of the session that creates it. Elsewhere `start` fails with `open_parent` and the parent link: tell the user to ask there.
3. After claiming, call `tree_create_saved_chat` for the item. Call `start_session` with exactly the returned `start_session` arguments; do not add or change any. Its first message is the item card with the operation token; the new chat binds itself to the item and shows its outline without starting the task.
4. Call `tree_step started` with `data: {node_id, chat_id}`, where `chat_id` is the session ID from `start_session`. The server verifies that folder, model, effort and permission mode match the parent. On a mismatch report it with the link; do not fix settings by hand or create another chat.
5. Give the link to the new chat; do not open it: the user may be working in another chat by the time creation finishes. Desktop also shows it nested under this chat in the sidebar.
6. If `start_session` fails, calling `tree_create_saved_chat` again returns the same arguments; never create a second chat for an item that already has one.

### Summaries and completion

1. Completed and closed items below the selected one are settled: their chats are archived, their saved results are already in the operation's `summaries`, and completion leaves their subtrees alone. Never message them. The operation lists the branch bottom-up; the current initiating chat is allowed to be busy.
2. For each linked chat except the current one whose item has no entry in `summaries`, obtain `tree_job purpose=summary`, send the exact prompt to its `chat_id` with `SendMessage` (or `send_message`), then call `tree_job wait_for=<node_id>` until `saved` is true. That chat saves its own fresh summary with `tree_summary`. If it is not saved after two waits, report the chat link and stop.
3. Process a parent only after its immediate child summaries are saved. Unstarted items remain unresolved. Each summary is one paragraph covering work, validation, decisions and unresolved issues, in the chat working language.
4. Summarize the selected current chat using child results and call `tree_summary`.
5. Deliver only to the immediate parent via `tree_job purpose=delivery`. If that parent is the initiating current chat, read the fresh result from `tree_job` and call `tree_step delivered`; do not message yourself. Otherwise send the exact prompt to the parent chat and call `tree_step delivered` once the message is delivered or queued. The parent acknowledges the result without implementing work or changing the tree.
6. `tree_step commit` completes only the selected item; its result carries `sync` with the archive of the item's chat. When that chat is the current one, show the final outline and archive it with `archive_session({session_id: "self"})` as the very last action: the conversation ends there. A failed archive does not undo completion; report it.
7. A new user message in a completed item's chat reopens the item automatically; complete it again only when asked.

### Service requests

A message from another session that contains a Chat Tree operation token is a service request of an approved operation: follow it exactly. For a summary, call `tree_summary` with the operation data and your paragraph and reply with the same paragraph. For a delivery, keep the result in mind and reply with one line acknowledging it.

### Delete and rebuild

- Initiate deletion outside the branch being deleted; rebuild in the chat of the rebuilt item's parent. Trusted plugin hooks prevent ordinary work in a reserved branch; do not bypass them.
- Rebuild first collects fresh summaries as above. Prepare one level of replacement items: preserve solved work in `parent_result` and reformulate unresolved tasks. Call `tree_step prepare_replacement`, then create a chat for each replacement exactly as in Start steps 3–4. Do not start implementation.
- Deliver the saved result to the immediate parent. `tree_step ready_to_delete` verifies replacement readiness and that the original branch is idle.
- For each original chat: archive it with `archive_session`, delete it with `delete_session` (Desktop asks the user to confirm), then call `tree_delete_saved_chat` with its exact operation node ID. A refused deletion stops the operation.
- After every original chat is actually deleted, `tree_step commit` removes the old structure. Resume the same partially completed operation and preserve collected results.
