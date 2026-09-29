# Ask, Notes, Palette and Themes Implementation Plan (Sub-project 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Ask and Notes placeholders of the run window with working tabs (experiment chat threads with a thread switcher, New thread, a model picker and the no-chat-harness state; a run notes editor with "Use as steer draft" and "Use as ask draft"), add the visible theme switcher, and extend the ⌘K palette with the new controls.

**Architecture:** No backend change: the run protocol already carries experiment chat (`query.chat`, `query.chat_thread_create`, `query.chat_options`, `chat` and `chat_thread_created` events, `RunSnapshot.chat_threads`; `src/server/api/service.py:257-290`), core-state already folds threads (`CoreState.chatThreads`, `chatTranscripts`), and plan 2 serves notes (`GET/PUT /api/notes/{run}`). `session.ts` gains chat requests; a pure `ask.ts` turns threads, captured `chat` events and pending asks into the Ask view; `notes.ts` holds the notes HTTP client, a reducer and the `useNote` hook; `theme.ts` holds the theme choice. `ui/Ask.tsx`, `ui/Notes.tsx` and a controlled `ui/Composer.tsx` render; `App.tsx` stays the only reader of the session and wires everything through plan 3's `PaneBody`, `RunHeader`, `RunComposer`, `runIntent` and `paletteInput`.

**Tech Stack:** React 19.1.1, TypeScript 5.8 (strict, `exactOptionalPropertyTypes`, `noUncheckedIndexedAccess`, `noPropertyAccessFromIndexSignature`), `@vibesys/core-state`, `@vibesys/backend-client`, `lucide-react` 1.47.0, bun test with the `node:test` API and `react-dom/server` static markup, Playwright 1.55 with plan 3's mocked `/ws` gateway, Biome 2.5. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-28-web-app-design.md`, section "5. Ask, notes, palette, themes", plus the constraints of section 3 (every element states one thing, the palette mirrors visible controls) and "Non-goals" (no controls the protocol lacks). Visual contract: `docs/superpowers/specs/2026-09-28-web-app/mockup.html`, screens `#ask`, `#thread`, `#model`, `#nochat`, `#notes`, `#palette` (full render set, when present: `/private/tmp/claude-501/-Users-grootbeat-Documents-vibesys/f98f6cbb-a02b-41d1-831c-fca4ee4bd0d9/scratchpad/mockups/app-{ask,thread,model,nochat,notes,palette}-1440-{dark,light}.png`).

**Builds on:** plan 3 (`docs/superpowers/plans/2026-09-28-3-app-shell.md`) and plan 2's notes endpoint. Tasks 1 to 6 anchor on plan 3 code through its Task 10 (`session.ts`, `ui-state.ts`, `Pane.tsx`, `TitleRow.tsx` with `MoreMenu` and `Popover`, `App.tsx` with `PaneBody`, `RunHeader`, `RunComposer`, `e2e/gateway.ts`, `e2e/screens.spec.ts`). Task 7 needs plan 3 Task 11 (`palette.ts`, `ui/Palette.tsx`, `runIntent`, `IntentContext`, `paletteInput`) to land first; Task 8 needs plan 3 Task 12 (the README `## Behavior` section). Plan 4 (setup UI) lands first; this plan touches no setup file and edits `App.tsx` and `main.tsx` only at the plan 3 anchors quoted in each step. If plan 4 moved an anchor, apply the same edit at its new place.

## Global Constraints

- Scope: `clients/web` only (plus `clients/web/README.md`). No Python change, no change to `clients/core-state` or `clients/backend-client`.
- Run controls: pause, resume, steer, stop only. Ask and Notes add no run control; "Use as steer draft" and "Use as ask draft" fill a composer and send nothing.
- Colours: plan 3's tokens in `theme.css`, verbatim (Radix Slate neutrals, one Indigo accent `--accent #3e63dd`, elevation by lightness). No colour literal in component CSS except plan 3's `#fff` on the primary button. Meaningful text at least 4.5:1 on its surface; nothing meaningful fainter than `--text-3`.
- Fonts: plan 3's system stacks (`--sans`, `--mono`); no font package.
- Themes: System (default, follows the OS through `light-dark()`), Light, Dark. Light and Dark set `data-theme` on `<html>`; System removes it. `?theme=light|dark` wins over the saved choice when the page opens (reviews and captures); choosing a theme in the app drops the parameter from the URL, so a reload keeps the choice.
- Every element states one thing; secondary detail (runtime, what a button does, why it is off) is a `title` hint. The palette mirrors visible controls; nothing is reachable only through it.
- UI copy: no em dashes; ellipsis is the single character `…`.
- Architecture: components under `src/ui` never import `session.ts` values (types only) and never call `fetch`; nothing under `src` except `*.test.*` imports a Node builtin; `pnpm check:ts-architecture` stays green.
- Biome limits: cognitive complexity at most 15, functions at most 80 lines, at most 6 parameters. No new `biome-ignore`.
- Tests: `node:test` API run by `bun test`, public functions only, Fakes (hand-written objects and functions), no mocks or monkeypatching, no sleeps or wall-clock dependence. Component tests render static markup.
- Export only what another module or test imports. If `pnpm check:knip` reports an unused export, remove the `export` keyword (or the symbol, when nothing uses it).
- Commands run from the `clients` directory of the worktree that holds plans 3 and 4 (plan 3 ran in `/Users/grootbeat/Documents/vibesys-wt/web-app-shell/clients`). Worktrees are outside the Bash sandbox's write allowlist: run file-writing commands, `git add`, `git commit` and Playwright (it binds 127.0.0.1:5173) with the sandbox disabled.
- Commits are conventional (`feat(web): …`) and end with the session's attribution line (the `-m "<the session's attribution line>"` in each commit step stands for it). Never `git stash`.
- Every UI task ends with screenshots through `e2e/screens.spec.ts`, each PNG opened and compared with the mockup screen named in the step.

## Review Focus

1. The backend records a chat answer to the journal (published on the stream) before it returns the `query.chat` response, so the answer usually arrives on the stream first. Expect it shown once, and the thread held until the response. (Task 1 "the stream and the response carry one answer once"; Task 2 "an answer recorded on the stream before its response shows once".)
2. A question pending on one thread while the user switches threads or asks in another: the answer lands in the thread it was asked in; only that thread's Send waits. (Task 1 "one question per thread at a time"; Task 2 "a pending question holds its thread…".)
3. Note edits followed at once by a tab switch, a run switch or closing the window: the text is saved to the run it belongs to, saves apply in order, and a note that failed to load is never overwritten. (Task 5 `noteReducer` tests "a stale run's results are ignored" and "nothing is editable before the note loads", the `serialSaver` order test, and the e2e tests "edits survive a tab switch" and "a note that fails to load is not editable".)
4. A run still starting reports no chat options, then does: Ask moves from "no chat harness" to the composer without a reload. (Task 2 harness test; Task 4 e2e "Ask offers chat once a starting run reports its options".)
5. The session lands on a different run while an ask is pending or drafts are filled: pending asks, the selected thread and both drafts reset. (Task 1 "a new run drops pending asks"; Task 3 "drafts and the Ask thread belong to one run".)

## Rulings (decisions this plan makes where the spec and mockup are silent)

- **No backend task.** Chat, threads and options exist in the protocol and core-state; notes exist in plan 2.
- **A model belongs to a thread.** `query.chat` has no model field; `query.chat_thread_create` takes provider and model. Picking a model in the chip starts a new thread on it (hint on the chip says so), as the TUI's `/model` does. Picking the current thread's model only closes the menu. The TUI's free-text custom model is left out.
- **One question per thread in flight.** Send waits (typing stays open) until the thread's answer arrives. The TUI's queue-and-join is left out.
- **No harness.** Without chat options the tab shows "This run offers no chat harness."; recorded threads stay readable above that line. The backend's read-only fallback summary for the default thread is not offered.
- **Chat options load on demand** (Ask, Notes or the palette open; again when the run status changes), not per bootstrap, so plan 3's query-budget tests stand.
- **Notes client is its own `NotesApi`**, not part of `HomeApi`, so this plan does not depend on plan 4's home client. The page's `?token=` is the home token; without one (replay, a bare `web live` gateway page) Notes says the home server keeps notes. Notes load on first view of each run, save 500 ms after typing stops, on blur, before another run's note loads, and on `pagehide`.
- **Drafts replace.** "Use as steer draft" and "Use as ask draft" replace the composer text (as the mockup and the TUI do), with line breaks folded to spaces because both composers are single-line inputs.
- **Theme switcher is visible in the ••• menu** (a "Theme" group of three radios) and mirrored in ⌘K. The choice is kept in `localStorage` (`vibesys.theme`), wrapped in try/catch; choosing one removes `?theme=` from the URL so a reload does not revert it.
- **No "load earlier" control in Ask.** It has no mockup screen; older questions appear as round backfills load the events around them.

## File Map

Created (all under `clients/web`):

| File | Responsibility |
|---|---|
| `src/ask.ts` | Ask view model: thread rows, messages, pending and streamed answer, harness state, model groups |
| `src/notes.ts` | `NotesApi` and `httpNotesApi`, `noteReducer`, `serialSaver`, `asDraft`, the `useNote` hook |
| `src/theme.ts` | `ThemeChoice`, labels, `initialTheme`, `applyTheme`, `savedTheme`, `saveTheme` |
| `src/ui/Composer.tsx` | Controlled single-line composer (input, extra controls, Send, error) |
| `src/ui/Ask.tsx` | Ask tab: thread head and switcher, thread, dock with model chip and menu |
| `src/ui/Notes.tsx` | Notes tab: editor, load and save states, the two draft buttons |
| Tests: `src/ask.test.ts`, `src/notes.test.ts`, `src/theme.test.ts`, `src/ui/Composer.test.tsx`, `src/ui/Ask.test.tsx`, `src/ui/Notes.test.tsx` | |

Modified: `src/session.ts`, `src/session.test.ts`, `src/ui-state.ts`, `src/ui-state.test.ts`, `src/ui/SteerComposer.tsx`, `src/ui/SteerComposer.test.tsx`, `src/ui/TitleRow.tsx`, `src/ui/TitleRow.test.tsx`, `src/ui/Pane.tsx` and `src/ui/Pane.test.tsx` (only if `Placeholder` becomes unused), `src/palette.ts`, `src/palette.test.ts`, `src/ui/Palette.tsx`, `src/App.tsx`, `src/main.tsx`, `src/window.css`, `e2e/gateway.ts`, `e2e/app.spec.ts`, `e2e/screens.spec.ts`, `README.md`.

Task order keeps the app working after every task: 1 and 2 add tested models, 3 makes the composers controlled, 4 to 7 replace the placeholders and add the theme and palette commands, 8 closes docs, gates and the review set.

---

### Task 1: Session: experiment chat requests and captured chat events

**Files:**
- Modify: `clients/web/src/session.ts`
- Test: `clients/web/src/session.test.ts`

**Interfaces:**
- Consumes: plan 3's `WorkspaceSession`, `capture()`, `commandMessage()`, `CAPTURED_TYPES`, `#runGeneration`.
- Produces:
  - `export interface SentAsk {id: string; threadId: string; text: string; afterSequence: number; answer: string | null; error: string | null}` (`id` is `ask-<n>`; `afterSequence` is the core sequence at send time; `answer` is set only for an answer the backend returned without recording it)
  - `WorkspaceState.asks: readonly SentAsk[]` (asks not yet seen recorded, oldest first; reset with the run)
  - `QueryName` gains `'chat_options'`; `WorkspaceState.queries.chat_options` starts idle (`loading: false`) and is loaded only by `session.load('chat_options')`
  - `CAPTURED_TYPES` additionally holds `chat`
  - `session.ask(text: string, threadId: string): boolean` (true when sent; false while that thread waits or the stream is not connected)
  - `session.createThread(selection: {provider: string; model: string} | null): Promise<string>` (the new thread id; rejects with the backend's message)

- [ ] **Step 1: Write the failing tests**

Append to `src/session.test.ts` (the `@vibesys/backend-client` and `./session.js` imports already hold what these use):

```ts
const chatEvent = (
  sequence: number,
  question: string,
  answer: string,
  thread: string | null = null,
): RunEvent => ({
  sequence,
  type: 'chat',
  timestamp: '2026-09-21T12:00:00Z',
  text: question,
  status: 'answered',
  agent_kind: 'chat',
  round_label: 'experiment-chat',
  chat_thread_id: thread,
  data: {kind: 'chat', answer, invocation_id: `inv-${sequence}`},
});

test('chat events are captured for the Ask tab', () => {
  assert.ok(CAPTURED_TYPES.has('chat'));
});

test('one question per thread at a time; the stream and the response carry one answer once', async () => {
  const client = new FakeClient();
  let reply: (value: ProtocolResponse) => void = () => {};
  const previous = client.replies;
  client.replies = input =>
    input.type === 'query.chat'
      ? new Promise(resolve => {
          reply = resolve;
        })
      : previous(input);
  const session = new WorkspaceSession(client);
  await session.start();
  assert.equal(session.ask('Why did round 3 fail?', 'default'), true);
  assert.equal(session.ask('And round 4?', 'default'), false, 'the default thread waits');
  assert.equal(session.ask('Other thread', 't2'), true, 'another thread is free');
  assert.deepEqual(
    client.requests.filter(request => request.type === 'query.chat'),
    [
      {type: 'query.chat', text: 'Why did round 3 fail?'},
      {type: 'query.chat', text: 'Other thread', thread_id: 't2'},
    ],
  );
  assert.deepEqual(
    session.getSnapshot().asks.map(ask => [ask.id, ask.threadId, ask.afterSequence]),
    [
      ['ask-1', 'default', 0],
      ['ask-2', 't2', 0],
    ],
  );
  const event = chatEvent(5, 'Other thread', 'Answered.', 't2');
  // The backend publishes the recorded answer before it returns the response.
  client.emit({type: 'event', event});
  reply(response({chat: {question: 'Other thread', answer: 'Answered.'}, events: [event]}));
  await settle();
  const state = session.getSnapshot();
  assert.equal(state.captured.filter(item => item.type === 'chat').length, 1);
  assert.deepEqual(
    state.asks.map(ask => ask.id),
    ['ask-1'],
    'the answered ask leaves; the other still waits',
  );
  await session.close();
});

test('an unrecorded answer stays on its ask; a failure keeps its message; a new run drops asks', async () => {
  const client = new FakeClient();
  const previous = client.replies;
  client.replies = async input => {
    if (input.type === 'query.chat' && 'thread_id' in input && input.thread_id === 't2')
      return response({
        chat: {question: 'Hi', answer: 'Thread t2 cannot answer right now.', thread_id: 't2'},
        events: [],
      });
    if (input.type === 'query.chat') throw new Error('gateway closed');
    return previous(input);
  };
  const session = new WorkspaceSession(client);
  await session.start();
  session.ask('Hi', 't2');
  session.ask('Why?', 'default');
  await settle();
  assert.deepEqual(
    session.getSnapshot().asks.map(ask => [ask.threadId, ask.answer, ask.error]),
    [
      ['t2', 'Thread t2 cannot answer right now.', null],
      ['default', null, 'gateway closed'],
    ],
  );
  assert.equal(session.ask('Again', 'default'), true, 'a failed ask does not hold its thread');
  client.emit({type: 'subscribed', run_id: 'run-2', request_id: 'sub', latest_sequence: 0});
  assert.deepEqual(session.getSnapshot().asks, []);
  await session.close();
});

test('threads are created with the chosen model, or the run default without one', async () => {
  const client = new FakeClient();
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.chat_thread_create'
      ? response({
          chat_thread: {
            thread_id: 't9',
            driver: 'agentshim',
            provider: 'claude',
            model: 'claude-sonnet-5',
          },
        })
      : previous(input);
  const session = new WorkspaceSession(client);
  await session.start();
  assert.equal(await session.createThread({provider: 'claude', model: 'claude-sonnet-5'}), 't9');
  assert.deepEqual(client.requests.at(-1), {
    type: 'query.chat_thread_create',
    provider: 'claude',
    model: 'claude-sonnet-5',
  });
  await session.createThread(null);
  assert.deepEqual(client.requests.at(-1), {type: 'query.chat_thread_create'});
  client.replies = async () => response();
  await assert.rejects(session.createThread(null), /no chat thread/);
  await session.close();
});

test('chat options are loaded on demand, never by a bootstrap', async () => {
  const client = new FakeClient();
  const previous = client.replies;
  client.replies = async input =>
    input.type === 'query.chat_options'
      ? response({chat_options: {providers: [{provider: 'claude', models: []}]}})
      : previous(input);
  const session = new WorkspaceSession(client);
  await session.start();
  await settle();
  assert.equal(queryCounts(client)['query.chat_options'], undefined);
  assert.deepEqual(session.getSnapshot().queries.chat_options, {
    response: null,
    loading: false,
    error: null,
  });
  await session.load('chat_options');
  assert.equal(
    session.getSnapshot().queries.chat_options.response?.chat_options?.providers?.[0]?.provider,
    'claude',
  );
  await session.close();
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm --filter @vibesys/web test`
Expected: FAIL: `session.ask` and `session.createThread` are not functions; `CAPTURED_TYPES` lacks `chat`; `queries.chat_options` is undefined.

- [ ] **Step 3: Implement the session additions**

In `src/session.ts`:

Add `DEFAULT_CHAT_THREAD_ID` to the `@vibesys/core-state` import list.

Replace `export type QueryName = 'experiments' | 'design' | 'performance';` with:

```ts
export type QueryName = 'experiments' | 'design' | 'performance' | 'chat_options';
```

Add after the `SentSteer` interface:

```ts
/**
 * A question sent to experiment chat. It leaves `asks` once its recorded `chat` event is captured;
 * `answer` is set only when the backend answered without recording (a thread that cannot answer
 * right now), `error` when the request failed.
 */
export interface SentAsk {
  id: string;
  threadId: string;
  text: string;
  /** The core sequence when it was sent: a recorded question after it may be this one. */
  afterSequence: number;
  answer: string | null;
  error: string | null;
}
```

In `WorkspaceState`, add after `sent`:

```ts
  /** Questions to experiment chat not yet seen recorded, oldest first. */
  asks: readonly SentAsk[];
```

Add `'chat',` to the `CAPTURED_TYPES` set (after `'control',`) and extend its doc comment's list with "chat answers".

Replace `const emptyQuery = (): QueryState => ({response: null, loading: true, error: null});` with:

```ts
const emptyQuery = (): QueryState => ({response: null, loading: true, error: null});
const emptyQueries = (): WorkspaceState['queries'] => ({
  experiments: emptyQuery(),
  design: emptyQuery(),
  performance: emptyQuery(),
  // Asked for where it is shown (Ask, Notes, the palette), never per bootstrap.
  chat_options: {response: null, loading: false, error: null},
});
```

Replace both occurrences of `queries: {experiments: emptyQuery(), design: emptyQuery(), performance: emptyQuery()},` (the initial `#state` and the run reset in `#onMessage`) with `queries: emptyQueries(),`. Add `asks: [],` after `sent: [],` in both places too.

Add the field `#askCount = 0;` after `#sentCount = 0;`.

Add after `designPatch`:

```ts
  /**
   * Asks experiment chat on one thread; true once the question is on its way. The answer arrives
   * as the recorded `chat` event (captured from the response and deduplicated with the stream by
   * sequence). One question per thread at a time; a failed one does not hold its thread.
   */
  ask(text: string, threadId: string): boolean {
    const waiting = this.#state.asks.some(
      ask => ask.threadId === threadId && ask.answer === null && ask.error === null,
    );
    if (waiting || this.#state.connection !== 'connected') return false;
    const sent: SentAsk = {
      id: `ask-${++this.#askCount}`,
      threadId,
      text,
      afterSequence: this.#state.core.sequence,
      answer: null,
      error: null,
    };
    this.#set({asks: [...this.#state.asks, sent]});
    void this.#answer(sent, this.#runGeneration);
    return true;
  }

  /** Creates a chat thread on `selection`, or on the run's own agent; resolves its id. */
  async createThread(selection: {provider: string; model: string} | null): Promise<string> {
    const response = await this.client.request({
      type: 'query.chat_thread_create',
      ...(selection ?? {}),
    });
    const id = response.chat_thread?.thread_id;
    if (id === undefined) throw new Error('The backend returned no chat thread.');
    return id;
  }

  async #answer(sent: SentAsk, generation: number): Promise<void> {
    try {
      const response = await this.client.request({
        type: 'query.chat',
        text: sent.text,
        ...(sent.threadId === DEFAULT_CHAT_THREAD_ID ? {} : {thread_id: sent.threadId}),
      });
      if (generation !== this.#runGeneration) return;
      const events = response.events ?? [];
      if (events.some(event => event.type === 'chat')) {
        this.#set({
          captured: capture(this.#state.captured, events),
          asks: this.#state.asks.filter(ask => ask.id !== sent.id),
        });
      } else {
        this.#settleAsk(sent.id, {answer: response.chat?.answer ?? 'No answer was returned.'});
      }
    } catch (error) {
      if (generation !== this.#runGeneration) return;
      this.#settleAsk(sent.id, {error: commandMessage(error)});
    }
  }

  #settleAsk(id: string, patch: Pick<SentAsk, 'answer'> | Pick<SentAsk, 'error'>): void {
    this.#set({asks: this.#state.asks.map(ask => (ask.id === id ? {...ask, ...patch} : ask))});
  }
```

`load(name)` already sends `query.${name}`, so `session.load('chat_options')` needs no change; `refresh()` keeps loading only the three run queries.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm --filter @vibesys/web test && pnpm --filter @vibesys/web check`
Expected: PASS, including plan 3's query-budget tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add web/src/session.ts web/src/session.test.ts
git commit -m "feat(web): experiment chat requests and captured chat answers in the session" -m "<the session's attribution line>"
```

---

### Task 2: Ask view model

**Files:**
- Create: `clients/web/src/ask.ts`
- Test: `clients/web/src/ask.test.ts`

**Interfaces:**
- Consumes: `SentAsk` (Task 1), `ChatThread`, `DEFAULT_CHAT_THREAD_ID`, `TranscriptEntry` (`@vibesys/core-state`), `ChatOptions`, `RunEvent` (`@vibesys/backend-client`), `prose` (`derive.ts`), `ProsePart` (`model.ts`).
- Produces:
  - `interface AskMessage {id: string; question: string; answer: ProsePart[][] | null; error: string | null}`
  - `interface ThreadRow {id: string; title: string; provider: string | null; model: string | null; count: number}`
  - `interface ModelGroup {provider: string; label: string; models: string[]}`
  - `interface AskView {harness: 'available' | 'checking' | 'none'; threads: ThreadRow[]; current: ThreadRow; messages: AskMessage[]; pending: boolean; streaming: ProsePart[][] | null; groups: ModelGroup[]}`
  - `interface AskInput {threads: readonly ChatThread[]; transcripts: Readonly<Record<string, readonly TranscriptEntry[]>>; captured: readonly RunEvent[]; asks: readonly SentAsk[]; options: ChatOptions | null; checking: boolean; selected: string}`
  - `askView(input: AskInput): AskView`

- [ ] **Step 1: Write the failing tests**

`src/ask.test.ts`:

```ts
import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import type {ChatOptions, RunEvent} from '@vibesys/backend-client';
import {type ChatThread, DEFAULT_CHAT_THREAD_ID, type TranscriptEntry} from '@vibesys/core-state';
import {type AskInput, askView} from './ask.js';
import type {SentAsk} from './session.js';

const chat = (sequence: number, question: string, answer: string, thread: string | null = null): RunEvent => ({
  sequence,
  type: 'chat',
  timestamp: '2026-09-25T14:00:00Z',
  text: question,
  status: 'answered',
  agent_kind: 'chat',
  round_label: 'experiment-chat',
  chat_thread_id: thread,
  data: {kind: 'chat', answer, invocation_id: `inv-${sequence}`},
});
const DEFAULT: ChatThread = {id: DEFAULT_CHAT_THREAD_ID, title: '', driver: null, provider: null, model: null};
const SONNET: ChatThread = {id: 't2', title: '', driver: 'agentshim', provider: 'claude', model: 'claude-sonnet-5'};
const OPTIONS: ChatOptions = {
  providers: [
    {
      provider: 'claude',
      models: [
        {model: 'claude-opus-5', source: 'run', default: true},
        {model: 'claude-sonnet-5', source: 'suggested'},
      ],
    },
    {provider: 'gemini', models: []},
  ],
};
const ask = (id: string, threadId: string, text: string, fields: Partial<SentAsk> = {}): SentAsk => ({
  id,
  threadId,
  text,
  afterSequence: 10,
  answer: null,
  error: null,
  ...fields,
});
const BASE: AskInput = {
  threads: [DEFAULT, SONNET],
  transcripts: {},
  captured: [],
  asks: [],
  options: OPTIONS,
  checking: false,
  selected: DEFAULT_CHAT_THREAD_ID,
};

test('threads: questions and answers per thread, titles from the first question, the implicit thread on the run default', () => {
  const view = askView({
    ...BASE,
    captured: [
      chat(11, 'Why did round 3 fail the judge?', 'On correctness.'),
      chat(12, 'What changed in round 5?\nIn detail.', 'Prefetch.', 't2'),
    ],
  });
  assert.deepEqual(
    view.threads.map(row => [row.id, row.title, row.model, row.count]),
    [
      ['default', 'Why did round 3 fail the judge?', 'claude-opus-5', 1],
      ['t2', 'What changed in round 5?', 'claude-sonnet-5', 1],
    ],
  );
  assert.deepEqual(
    view.messages.map(message => [message.question, message.answer]),
    [['Why did round 3 fail the judge?', [[{kind: 'text', text: 'On correctness.'}]]]],
  );
  assert.equal(view.harness, 'available');
  assert.deepEqual(view.groups, [
    {provider: 'claude', label: 'Claude Code harness', models: ['claude-opus-5', 'claude-sonnet-5']},
  ]);
});

test('a pending question holds its thread and shows the streamed answer; other threads stay free', () => {
  const streamed: TranscriptEntry = {id: 'e1', kind: 'assistant', content: 'Looking at the verdict', turnId: 'inv-9'};
  const input: AskInput = {...BASE, asks: [ask('ask-1', 'default', 'Why?')], transcripts: {default: [streamed]}};
  const view = askView(input);
  assert.equal(view.pending, true);
  assert.deepEqual(view.messages.map(message => [message.question, message.answer, message.error]), [['Why?', null, null]]);
  assert.deepEqual(view.streaming, [[{kind: 'text', text: 'Looking at the verdict'}]]);
  const other = askView({...input, selected: 't2'});
  assert.deepEqual([other.pending, other.messages.length, other.streaming], [false, 0, null]);
});

test('an answer recorded on the stream before its response shows once; unrecorded answers and failures stay', () => {
  const both = askView({...BASE, asks: [ask('ask-1', 'default', 'Why?')], captured: [chat(11, 'Why?', 'Because.')]});
  assert.deepEqual(both.messages.map(message => message.id), ['chat-11']);
  assert.equal(both.pending, true, 'Send waits for the response all the same');
  const older = askView({...BASE, asks: [ask('ask-1', 'default', 'Why?')], captured: [chat(9, 'Why?', 'Earlier.')]});
  assert.deepEqual(older.messages.map(message => message.id), ['chat-9', 'ask-1'], 'an earlier identical question is not this one');
  const local = askView({
    ...BASE,
    selected: 't2',
    asks: [
      ask('ask-2', 't2', 'Hi', {answer: 'Thread t2 cannot answer right now.'}),
      ask('ask-3', 't2', 'Again', {error: 'gateway closed'}),
    ],
  });
  assert.deepEqual(
    local.messages.map(message => [message.question, message.answer !== null, message.error]),
    [
      ['Hi', true, null],
      ['Again', false, 'gateway closed'],
    ],
  );
  assert.equal(local.pending, false);
});

test('a thread created a moment ago is current before its record arrives', () => {
  const view = askView({...BASE, selected: 't9'});
  assert.deepEqual([view.current.id, view.current.title, view.current.model], ['t9', 'New thread', 'claude-opus-5']);
  assert.deepEqual(view.threads.map(row => row.id), ['default', 't2', 't9']);
});

test('the harness: available with a provider, checking until the options query answers, none otherwise', () => {
  assert.equal(askView({...BASE, options: null, checking: true}).harness, 'checking');
  assert.equal(askView({...BASE, options: null}).harness, 'none');
  assert.equal(askView({...BASE, options: {providers: []}}).harness, 'none');
  assert.equal(askView({...BASE, options: null}).current.model, null);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm --filter @vibesys/web test`
Expected: FAIL: `./ask.js` not found.

- [ ] **Step 3: Implement `src/ask.ts`**

```ts
/**
 * The Ask tab: experiment-chat threads, their questions and answers, and the models the run
 * offers. Questions come from recorded `chat` events the session captured, plus the session's own
 * asks not yet seen recorded.
 */
import type {ChatOptions, RunEvent} from '@vibesys/backend-client';
import {type ChatThread, DEFAULT_CHAT_THREAD_ID, type TranscriptEntry} from '@vibesys/core-state';
import {prose} from './derive.js';
import type {ProsePart} from './model.js';
import type {SentAsk} from './session.js';

export interface AskMessage {
  id: string;
  question: string;
  answer: ProsePart[][] | null;
  error: string | null;
}

export interface ThreadRow {
  id: string;
  title: string;
  /** Who answers: the thread's own selection, or the run's default for the implicit thread. */
  provider: string | null;
  model: string | null;
  count: number;
}

export interface ModelGroup {
  provider: string;
  label: string;
  models: string[];
}

export interface AskView {
  harness: 'available' | 'checking' | 'none';
  threads: ThreadRow[];
  current: ThreadRow;
  messages: AskMessage[];
  /** A question on this thread waits for its answer: Send holds until it arrives. */
  pending: boolean;
  /** The answer streamed so far while the question waits. */
  streaming: ProsePart[][] | null;
  groups: ModelGroup[];
}

export interface AskInput {
  threads: readonly ChatThread[];
  transcripts: Readonly<Record<string, readonly TranscriptEntry[]>>;
  captured: readonly RunEvent[];
  asks: readonly SentAsk[];
  options: ChatOptions | null;
  /** The options query has not answered yet. */
  checking: boolean;
  selected: string;
}

const HARNESS_NAMES: Readonly<Record<string, string>> = {
  claude: 'Claude Code',
  codex: 'Codex',
  gemini: 'Gemini',
  opencode: 'Opencode',
};

const waiting = (ask: SentAsk): boolean => ask.answer === null && ask.error === null;

function recordedIn(captured: readonly RunEvent[], thread: string): RunEvent[] {
  return captured.filter(
    event => event.type === 'chat' && (event.chat_thread_id ?? DEFAULT_CHAT_THREAD_ID) === thread,
  );
}

/** Asks not yet seen as a question recorded after they were sent (the stream can beat the response). */
function unrecorded(asks: readonly SentAsk[], recorded: readonly RunEvent[]): SentAsk[] {
  const claimed = new Set<number>();
  return asks.filter(ask => {
    const match = recorded.find(event => {
      const sequence = event.sequence ?? 0;
      return event.text === ask.text && sequence > ask.afterSequence && !claimed.has(sequence);
    });
    if (match === undefined) return true;
    claimed.add(match.sequence ?? 0);
    return false;
  });
}

function answerOf(event: RunEvent): string {
  const data = event.data;
  return data?.kind === 'chat' ? data.answer : '';
}

function messagesOf(input: AskInput, thread: string): AskMessage[] {
  const recorded = recordedIn(input.captured, thread);
  const answered = recorded.map(event => ({
    id: `chat-${event.sequence ?? 0}`,
    question: event.text ?? '',
    answer: prose(answerOf(event)),
    error: null,
  }));
  const mine = input.asks.filter(ask => ask.threadId === thread);
  const local = unrecorded(mine, recorded).map(ask => ({
    id: ask.id,
    question: ask.text,
    answer: ask.answer === null ? null : prose(ask.answer),
    error: ask.error,
  }));
  return [...answered, ...local];
}

function runDefault(options: ChatOptions | null): Pick<ThreadRow, 'provider' | 'model'> {
  for (const group of options?.providers ?? []) {
    const option = group.models?.find(candidate => candidate.default === true);
    if (option !== undefined) return {provider: group.provider, model: option.model};
  }
  return {provider: null, model: null};
}

function rowOf(input: AskInput, thread: ChatThread): ThreadRow {
  const messages = messagesOf(input, thread.id);
  const runtime =
    thread.model === null
      ? runDefault(input.options)
      : {provider: thread.provider, model: thread.model};
  const first = messages[0]?.question.split('\n')[0];
  return {id: thread.id, title: thread.title || first || 'New thread', ...runtime, count: messages.length};
}

function modelGroups(options: ChatOptions | null): ModelGroup[] {
  return (options?.providers ?? [])
    .map(group => ({
      provider: group.provider,
      label: `${HARNESS_NAMES[group.provider] ?? group.provider} harness`,
      models: (group.models ?? []).map(option => option.model),
    }))
    .filter(group => group.models.length > 0);
}

export function askView(input: AskInput): AskView {
  const selected: ChatThread = input.threads.find(thread => thread.id === input.selected) ?? {
    id: input.selected,
    title: '',
    driver: null,
    provider: null,
    model: null,
  };
  // A thread created a moment ago can be selected before its record reaches the page.
  const threads = input.threads.includes(selected) ? input.threads : [...input.threads, selected];
  const pending = input.asks.some(ask => ask.threadId === selected.id && waiting(ask));
  const open = input.transcripts[selected.id]?.at(-1);
  const streamed = pending && open?.kind === 'assistant' && open.turnId !== undefined;
  const offered = (input.options?.providers ?? []).length > 0;
  return {
    harness: offered ? 'available' : input.checking ? 'checking' : 'none',
    threads: threads.map(thread => rowOf(input, thread)),
    current: rowOf(input, selected),
    messages: messagesOf(input, selected.id),
    pending,
    streaming: streamed && open !== undefined ? prose(open.content) : null,
    groups: modelGroups(input.options),
  };
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm --filter @vibesys/web test && pnpm --filter @vibesys/web check && pnpm exec biome check --write web`
Expected: PASS; biome reformats the long lines and reports nothing.

- [ ] **Step 5: Commit**

```bash
git add web/src/ask.ts web/src/ask.test.ts
git commit -m "feat(web): derive Ask threads, questions and answers from chat events" -m "<the session's attribution line>"
```

---

### Task 3: Controlled composers, drafts and the Ask thread in the UI state

**Files:**
- Create: `clients/web/src/ui/Composer.tsx`
- Modify: `clients/web/src/ui-state.ts`, `clients/web/src/ui/SteerComposer.tsx`, `clients/web/src/ui/TitleRow.tsx`, `clients/web/src/App.tsx`
- Test: `clients/web/src/ui-state.test.ts`, `clients/web/src/ui/Composer.test.tsx`, `clients/web/src/ui/SteerComposer.test.tsx`

**Interfaces:**
- Consumes: plan 3's `UiState`, `uiReducer`, `forRun`, `INITIAL_UI`, `MoreMenu`, `RunComposer`.
- Produces:
  - `ui-state.ts`: `type Menu = 'more' | 'stop' | 'thread' | 'model' | null`; `UiState.thread: string` (initially `DEFAULT_CHAT_THREAD_ID`); `UiState.drafts: {steer: string; ask: string}`; actions `{type: 'thread'; id: string}` (also closes a menu) and `{type: 'draft'; target: 'steer' | 'ask'; text: string}`; `forRun` resets `thread` and `drafts`
  - `ui/Composer.tsx`: `Composer({id, label, placeholder, draft, onDraft, disabled, held?, error, onSend, children?})`, where `onSend: (text: string) => Promise<boolean>` and the draft clears when it resolves true, unless it changed meanwhile
  - `ui/SteerComposer.tsx`: `SteerComposer({disabled, reason, error, draft, onDraft, onSend})` (the dock around a `Composer` with `id="steer"`)
  - `MoreMenu` is pressed only for its own menus (`more`, `stop`)

- [ ] **Step 1: Write the failing tests**

Append to `src/ui-state.test.ts`:

```ts
test('drafts and the Ask thread belong to one run', () => {
  let state = forRun(INITIAL_UI, 'run-1');
  state = uiReducer(state, {type: 'draft', target: 'steer', text: 'Measure first.'});
  state = uiReducer(state, {type: 'draft', target: 'ask', text: 'Why?'});
  state = uiReducer({...state, menu: 'thread'}, {type: 'thread', id: 't2'});
  assert.deepEqual(
    [state.drafts, state.thread, state.menu],
    [{steer: 'Measure first.', ask: 'Why?'}, 't2', null],
  );
  const next = forRun(state, 'run-2');
  assert.deepEqual([next.drafts, next.thread], [{steer: '', ask: ''}, 'default']);
});
```

Create `src/ui/Composer.test.tsx`:

```tsx
import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {Composer} from './Composer.js';

test('a held composer keeps typing open but waits to send; extra controls sit before Send', () => {
  const html = renderToStaticMarkup(
    <Composer
      id="ask"
      label="Ask about this run"
      placeholder="Ask about this run…"
      draft="And round 4?"
      onDraft={() => {}}
      disabled={false}
      held
      error={null}
      onSend={async () => true}
    >
      <button type="button" className="mchip">
        claude-opus-5
      </button>
    </Composer>,
  );
  assert.match(html, /<input id="ask" aria-label="Ask about this run"[^>]*value="And round 4\?"/);
  assert.doesNotMatch(html, /<input[^>]*disabled/);
  assert.match(
    html,
    /class="mchip">claude-opus-5<\/button><button type="button" class="send" aria-label="Send" title="Waiting for the answer" disabled="">/,
  );
});
```

In `src/ui/SteerComposer.test.tsx`, add `draft=""` and `onDraft={() => {}}` to the rendered `<SteerComposer …/>`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm --filter @vibesys/web test`
Expected: FAIL: `drafts` is undefined; `./Composer.js` not found.

- [ ] **Step 3: Implement**

In `src/ui-state.ts`:

Add at the top: `import {DEFAULT_CHAT_THREAD_ID} from '@vibesys/core-state';`

Replace `export type Menu = 'more' | 'stop' | null;` with:

```ts
/** The one open popover: the ••• menu, the Stop confirmation, or Ask's thread and model menus. */
export type Menu = 'more' | 'stop' | 'thread' | 'model' | null;
```

Add to `UiState` after `experimentsView`:

```ts
  /** The Ask thread on screen. */
  thread: string;
  /** Unsent composer text; a note can replace either (drafts only, nothing is sent). */
  drafts: {steer: string; ask: string};
```

Add to `UiAction`:

```ts
  | {type: 'thread'; id: string}
  | {type: 'draft'; target: 'steer' | 'ask'; text: string}
```

Add to `INITIAL_UI`: `thread: DEFAULT_CHAT_THREAD_ID,` and `drafts: {steer: '', ask: ''},`.

In `forRun`, add to the returned object: `thread: DEFAULT_CHAT_THREAD_ID,` and `drafts: INITIAL_UI.drafts,`, and extend its doc comment's list of run-bound state with "the Ask thread, drafts".

Add two cases to `uiReducer`:

```ts
    case 'thread':
      return {...state, thread: action.id, menu: null};
    case 'draft':
      return {...state, drafts: {...state.drafts, [action.target]: action.text}};
```

Create `src/ui/Composer.tsx`:

```tsx
import {ArrowUp} from 'lucide-react';
import {type ReactNode, useRef, useState} from 'react';

export interface ComposerProps {
  id: string;
  label: string;
  placeholder: string;
  draft: string;
  onDraft: (text: string) => void;
  disabled: boolean;
  /** Typing stays open but Send waits (an answer on this thread is pending). */
  held?: boolean;
  error: string | null;
  /** Resolves true when the text was taken; the draft clears then, unless it changed meanwhile. */
  onSend: (text: string) => Promise<boolean>;
  /** Controls between the input and Send (Ask's model chip). */
  children?: ReactNode;
}

export function Composer(props: ComposerProps) {
  const {id, label, placeholder, draft, onDraft, disabled, held = false, error, onSend} = props;
  const [sending, setSending] = useState(false);
  const latest = useRef(draft);
  latest.current = draft;
  const ready = draft.trim() !== '' && !disabled && !held && !sending;
  async function send() {
    if (!ready) return;
    const submitted = draft;
    setSending(true);
    try {
      // Text typed while the acknowledgment was pending stays.
      if ((await onSend(submitted.trim())) && latest.current === submitted) onDraft('');
    } finally {
      setSending(false);
    }
  }
  return (
    <>
      <div className={disabled ? 'pill off' : 'pill'}>
        <input
          id={id}
          aria-label={label}
          placeholder={placeholder}
          value={draft}
          disabled={disabled}
          onChange={event => onDraft(event.target.value)}
          onKeyDown={event => {
            // Safari ends an IME composition before the Enter that commits it; keyCode 229 marks it.
            const composing = event.nativeEvent.isComposing || event.keyCode === 229;
            if (event.key === 'Enter' && !composing) void send();
          }}
        />
        {props.children}
        <button
          type="button"
          className={ready ? 'send ready' : 'send'}
          aria-label="Send"
          title={held ? 'Waiting for the answer' : 'Send (↵)'}
          disabled={!ready}
          onClick={() => void send()}
        >
          <ArrowUp size={16} strokeWidth={1.5} aria-hidden />
        </button>
      </div>
      {error === null ? null : (
        <p className="hint bad" role="alert">
          {error}
        </p>
      )}
    </>
  );
}
```

Replace the whole of `src/ui/SteerComposer.tsx` with:

```tsx
import {Composer} from './Composer.js';

export interface SteerComposerProps {
  disabled: boolean;
  /** Why steering is off, shown as the placeholder; null while it is on. */
  reason: string | null;
  error: string | null;
  draft: string;
  onDraft: (text: string) => void;
  /** Resolves true when the backend acknowledged the steer; the draft clears then. */
  onSend: (text: string) => Promise<boolean>;
}

export function SteerComposer({reason, ...rest}: SteerComposerProps) {
  return (
    <div className="dock">
      <div className="inner">
        <Composer
          id="steer"
          label="Steer the next agent call"
          placeholder={reason ?? 'Steer the next agent call…'}
          {...rest}
        />
      </div>
    </div>
  );
}
```

In `src/ui/TitleRow.tsx`, inside `MoreMenu`, replace `className={menu === null ? 'iconbtn' : 'iconbtn on'}` with `className={menu === 'more' || menu === 'stop' ? 'iconbtn on' : 'iconbtn'}` and `onClick={() => onMenu(menu === null ? 'more' : null)}` with `onClick={() => onMenu(menu === 'more' ? null : 'more')}` (another popover being open must not make ••• look pressed or swallow its click).

In `src/App.tsx`, `RunComposer`: replace `function RunComposer({state, view, session}: SectionProps) {` with `function RunComposer({state, view, ui, dispatch, session}: SectionProps) {`, and add these two props to its `<SteerComposer …/>` element:

```tsx
      draft={ui.drafts.steer}
      onDraft={text => dispatch({type: 'draft', target: 'steer', text})}
```

- [ ] **Step 4: Run everything**

Run: `pnpm --filter @vibesys/web test && pnpm --filter @vibesys/web check && pnpm exec biome check --write web && pnpm check:knip && pnpm --filter @vibesys/web test:e2e`
Expected: all PASS; plan 3's steer e2e test still passes on the controlled composer.

- [ ] **Step 5: Commit**

```bash
git add web/src/ui-state.ts web/src/ui-state.test.ts web/src/ui/Composer.tsx web/src/ui/Composer.test.tsx web/src/ui/SteerComposer.tsx web/src/ui/SteerComposer.test.tsx web/src/ui/TitleRow.tsx web/src/App.tsx
git commit -m "refactor(web): controlled composers with run-scoped drafts and Ask thread" -m "<the session's attribution line>"
```

---

### Task 4: Ask tab

**Files:**
- Create: `clients/web/src/ui/Ask.tsx`
- Modify: `clients/web/src/ui/TitleRow.tsx` (export `Popover`), `clients/web/src/App.tsx`, `clients/web/src/window.css`, `clients/web/e2e/gateway.ts`, `clients/web/e2e/app.spec.ts`, `clients/web/e2e/screens.spec.ts`
- Test: `clients/web/src/ui/Ask.test.tsx`

**Interfaces:**
- Consumes: `askView`, `AskView`, `ThreadRow`, `ModelGroup`, `AskMessage` (Task 2); `session.ask`, `session.createThread`, `session.load('chat_options')`, `SentAsk` (Task 1); `Composer`, `Menu`, `thread` and `draft` actions (Task 3); plan 3's `PaneHead`, `Prose`, `Popover`, `PaneHostProps`, `PaneBody`, `RunPane`.
- Produces:
  - `ui/TitleRow.tsx`: `export function Popover({label, onClose, children})` (unchanged body)
  - `ui/Ask.tsx`: `AskTab(props: AskTabProps)` with `interface AskTabProps {view: AskView; menu: Menu; draft: string; reason: string | null; error: string | null; onMenu: (menu: Menu) => void; onThread: (id: string) => void; onNewThread: (selection: {provider: string; model: string} | null) => void; onDraft: (text: string) => void; onSend: (text: string) => Promise<boolean>}`; the composer input has `id="ask"` and the accessible name `Ask about this run`; the thread button is named `Thread: <title>`, the chip `Chat model`, the popovers `Threads` and `Chat model`
  - `App.tsx`: `interface AskHost {view: AskView; error: string | null; start: (selection: {provider: string; model: string} | null) => void}`, `useAsk(session, state, ui, dispatch): AskHost`, `PaneHostProps.ask: AskHost`, `askTab(props: PaneHostProps)`
  - `e2e/gateway.ts`: `mockGateway(page, {through?, status?, chat?: 'off' | 'late'})` answers `query.chat_options`, `query.chat_thread_create` and `query.chat`; `GatewayRequest` gains `thread_id`, `provider`, `model`, `title`
  - `e2e/screens.spec.ts`: `Screen.chat?: 'off'`, helper `openPane(page, tab)`, screens `ask`, `ask-thread`, `ask-model`, `ask-nochat`

- [ ] **Step 1: Write the failing component test**

`src/ui/Ask.test.tsx`:

```tsx
import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {AskView, ThreadRow} from '../ask.js';
import {AskTab, type AskTabProps} from './Ask.js';

const row = (id: string, title: string, count: number): ThreadRow => ({
  id,
  title,
  provider: 'claude',
  model: 'claude-opus-5',
  count,
});
const VIEW: AskView = {
  harness: 'available',
  threads: [row('default', 'Why did round 3 fail the judge?', 2), row('t2', 'New thread', 0)],
  current: row('default', 'Why did round 3 fail the judge?', 2),
  messages: [
    {
      id: 'chat-5',
      question: 'Why did round 3 fail the judge?',
      answer: [[{kind: 'text', text: 'On correctness, not performance.'}]],
      error: null,
    },
    {id: 'ask-2', question: 'And round 4?', answer: null, error: null},
  ],
  pending: true,
  streaming: null,
  groups: [{provider: 'claude', label: 'Claude Code harness', models: ['claude-opus-5', 'claude-sonnet-5']}],
};
const props = (overrides: Partial<AskTabProps> = {}): AskTabProps => ({
  view: VIEW,
  menu: null,
  draft: '',
  reason: null,
  error: null,
  onMenu: () => {},
  onThread: () => {},
  onNewThread: () => {},
  onDraft: () => {},
  onSend: async () => true,
  ...overrides,
});

test('a thread: its title opens the switcher, questions and answers, the pending one answering, the model chip', () => {
  const html = renderToStaticMarkup(<AskTab {...props()} />);
  assert.match(html, /aria-label="Thread: Why did round 3 fail the judge\?"/);
  assert.match(html, /aria-label="New thread"/);
  assert.match(html, /<div class="human">Why did round 3 fail the judge\?<\/div>/);
  assert.match(html, /<div class="who2">claude-opus-5<\/div><p>On correctness, not performance\.<\/p>/);
  assert.match(html, /Answering…/);
  assert.match(html, /aria-label="Chat model"[^>]*>claude-opus-5/);
  assert.match(html, /class="send" aria-label="Send" title="Waiting for the answer" disabled=""/);
});

test('menus: threads with their counts, models grouped by harness with the current one checked', () => {
  const threads = renderToStaticMarkup(<AskTab {...props({menu: 'thread'})} />);
  assert.match(threads, /<div class="gh">2 threads<\/div>/);
  assert.match(threads, /role="menuitemradio" aria-checked="true" aria-label="Why did round 3 fail the judge\?, 2 questions"/);
  const models = renderToStaticMarkup(<AskTab {...props({menu: 'model'})} />);
  assert.match(models, /<div class="gh">Claude Code harness<\/div>/);
  assert.match(models, /aria-checked="true" class="it on">claude-opus-5/);
  assert.match(models, /aria-checked="false" class="it">claude-sonnet-5/);
});

test('without a chat harness the tab says so; recorded threads stay readable', () => {
  const empty = row('default', 'New thread', 0);
  const none: AskView = {...VIEW, harness: 'none', threads: [empty], current: empty, messages: [], pending: false};
  const html = renderToStaticMarkup(<AskTab {...props({view: none})} />);
  assert.match(html, /This run offers no chat harness\./);
  assert.doesNotMatch(html, /Ask about this run/);
  const history = renderToStaticMarkup(<AskTab {...props({view: {...VIEW, harness: 'none', pending: false}})} />);
  assert.match(history, /class="human"/);
  assert.match(history, /This run offers no chat harness\./);
  assert.doesNotMatch(history, /aria-label="New thread"/);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pnpm --filter @vibesys/web test`
Expected: FAIL: `./Ask.js` not found.

- [ ] **Step 3: Implement `Popover` export and `src/ui/Ask.tsx`**

In `src/ui/TitleRow.tsx`, change `function Popover({` to `export function Popover({`.

`src/ui/Ask.tsx`:

```tsx
import {Check, ChevronDown, Plus} from 'lucide-react';
import type {AskMessage, AskView, ModelGroup, ThreadRow} from '../ask.js';
import type {ProsePart} from '../model.js';
import type {Menu} from '../ui-state.js';
import {Composer} from './Composer.js';
import {PaneHead} from './Pane.js';
import {Prose} from './Prose.js';
import {Popover} from './TitleRow.js';

type Selection = {provider: string; model: string};

export interface AskTabProps {
  view: AskView;
  menu: Menu;
  draft: string;
  /** Why asking is off (the connection is down), or null. */
  reason: string | null;
  /** A thread that could not be started, or null. */
  error: string | null;
  onMenu: (menu: Menu) => void;
  onThread: (id: string) => void;
  onNewThread: (selection: Selection | null) => void;
  onDraft: (text: string) => void;
  onSend: (text: string) => Promise<boolean>;
}

const NO_HARNESS = 'This run offers no chat harness.';

const runtime = (row: ThreadRow): string | null =>
  [row.provider, row.model].filter(part => part !== null).join(' · ') || null;

export function AskTab(props: AskTabProps) {
  const {view} = props;
  if (view.harness !== 'available' && view.threads.every(thread => thread.count === 0)) {
    return (
      <>
        <PaneHead scope="Run" />
        <p className="empty1">{view.harness === 'checking' ? 'Checking the chat harness…' : NO_HARNESS}</p>
      </>
    );
  }
  return (
    <>
      <ThreadHead {...props} />
      <Thread view={view} />
      {view.harness === 'available' ? <AskDock {...props} /> : null}
      {view.harness === 'none' ? <p className="empty1">{NO_HARNESS}</p> : null}
    </>
  );
}

function ThreadHead({view, menu, onMenu, onThread, onNewThread}: AskTabProps) {
  const {current, threads} = view;
  const available = view.harness === 'available';
  const answeredBy = runtime(current);
  return (
    <div className="phead askhead">
      <span className="scope">Run</span>
      <button
        type="button"
        className={menu === 'thread' ? 'disc on' : 'disc'}
        aria-label={`Thread: ${current.title}`}
        aria-haspopup="menu"
        aria-expanded={menu === 'thread'}
        title={answeredBy === null ? 'Switch thread' : `Switch thread (answered by ${answeredBy})`}
        onClick={() => onMenu(menu === 'thread' ? null : 'thread')}
      >
        <span className="ttl">{current.title}</span>
        <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
      </button>
      {available ? (
        <span className="r">
          <button
            type="button"
            className="iconbtn"
            title="New thread"
            aria-label="New thread"
            onClick={() => onNewThread(null)}
          >
            <Plus size={16} strokeWidth={1.5} aria-hidden />
          </button>
        </span>
      ) : null}
      {menu === 'thread' ? (
        <Popover label="Threads" onClose={() => onMenu(null)}>
          <div className="gh">{threads.length === 1 ? '1 thread' : `${threads.length} threads`}</div>
          {threads.map(thread => (
            <button
              key={thread.id}
              type="button"
              role="menuitemradio"
              aria-checked={thread.id === current.id}
              aria-label={`${thread.title}, ${thread.count} ${thread.count === 1 ? 'question' : 'questions'}`}
              className={thread.id === current.id ? 'it on' : 'it'}
              title={runtime(thread) ?? undefined}
              onClick={() => onThread(thread.id)}
            >
              <span className="ttl">{thread.title}</span>
              <span className="d">{thread.count}</span>
            </button>
          ))}
          {available ? (
            <>
              <div className="sepl" />
              <button type="button" role="menuitem" className="it" onClick={() => onNewThread(null)}>
                <Plus size={14} strokeWidth={1.5} aria-hidden />
                New thread
              </button>
            </>
          ) : null}
        </Popover>
      ) : null}
    </div>
  );
}

function Thread({view}: Pick<AskTabProps, 'view'>) {
  const who = view.current.model ?? 'Answer';
  return (
    <div className="thread">
      {view.messages.length === 0 ? (
        <p className="t2">Ask about progress, a failure, or what a hypothesis changed.</p>
      ) : null}
      {view.messages.map(message => (
        <Exchange key={message.id} message={message} who={who} streaming={view.streaming} />
      ))}
    </div>
  );
}

function Exchange({message, who, streaming}: {message: AskMessage; who: string; streaming: ProsePart[][] | null}) {
  return (
    <>
      <div className="human">{message.question}</div>
      {message.error !== null ? (
        <p className="hint bad" role="alert">{`Not answered: ${message.error}`}</p>
      ) : (
        <div className="answer">
          <div className="who2">{who}</div>
          {message.answer !== null ? (
            <Prose paragraphs={message.answer} />
          ) : streaming !== null ? (
            <Prose paragraphs={streaming} />
          ) : (
            <p className="t2">
              <span className="spin" /> Answering…
            </p>
          )}
        </div>
      )}
    </>
  );
}

function AskDock({view, menu, draft, reason, error, onMenu, onNewThread, onDraft, onSend}: AskTabProps) {
  const {current} = view;
  const pick = (provider: string, model: string) =>
    provider === current.provider && model === current.model ? onMenu(null) : onNewThread({provider, model});
  return (
    <div className="pdock">
      <Composer
        id="ask"
        label="Ask about this run"
        placeholder={reason ?? 'Ask about this run…'}
        draft={draft}
        onDraft={onDraft}
        disabled={reason !== null}
        held={view.pending}
        error={error}
        onSend={onSend}
      >
        <button
          type="button"
          className="mchip"
          aria-label="Chat model"
          aria-haspopup="menu"
          aria-expanded={menu === 'model'}
          title="Choosing a model starts a new thread"
          onClick={() => onMenu(menu === 'model' ? null : 'model')}
        >
          {current.model ?? 'Model'}
          <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
        </button>
      </Composer>
      {menu === 'model' ? (
        <ModelMenu groups={view.groups} current={current} onPick={pick} onClose={() => onMenu(null)} />
      ) : null}
    </div>
  );
}

function ModelMenu(props: {
  groups: ModelGroup[];
  current: ThreadRow;
  onPick: (provider: string, model: string) => void;
  onClose: () => void;
}) {
  const {groups, current, onPick, onClose} = props;
  return (
    <Popover label="Chat model" onClose={onClose}>
      {groups.map(group => (
        <div key={group.provider}>
          <div className="gh">{group.label}</div>
          {group.models.map(model => {
            const on = group.provider === current.provider && model === current.model;
            return (
              <button
                key={model}
                type="button"
                role="menuitemradio"
                aria-checked={on}
                className={on ? 'it on' : 'it'}
                onClick={() => onPick(group.provider, model)}
              >
                {model}
                {on ? <Check size={14} strokeWidth={1.5} className="d" aria-hidden /> : null}
              </button>
            );
          })}
        </div>
      ))}
    </Popover>
  );
}
```

Append to `src/window.css`:

```css
/* ask */
.askhead, .pdock { position: relative; }
.askhead .disc { min-width: 0; max-width: 100%; overflow: hidden; }
.askhead .ttl, .pop .it .ttl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.askhead .pop { top: 36px; left: 12px; right: auto; width: 300px; }
.pdock .pop { top: auto; bottom: 60px; right: 12px; width: 250px; }
.pop .gh { font-size: 11px; color: var(--text-3); padding: 6px 10px 2px; }
.pop .it .d { margin-left: auto; color: var(--text-2); font-size: 12px; }
.pop .it.on { background: var(--bg-hover); }
.thread { flex: 1; overflow: auto; padding: 16px; }
.thread .human { margin-top: 0; }
.answer { margin: 8px 0 18px; }
.answer .who2 { font-size: 12px; color: var(--text-2); margin-bottom: 4px; }
.answer p { margin: 0 0 8px; }
.answer .spin { vertical-align: -1px; }
.pdock { flex: none; padding: 0 12px 12px; }
.pdock .hint { margin-left: 16px; }
.mchip { height: 28px; padding: 0 8px; border-radius: 14px; display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-2); flex: none; }
.mchip:hover { background: var(--bg-hover); color: var(--text-1); }
```

- [ ] **Step 4: Run the component test**

Run: `pnpm --filter @vibesys/web test`
Expected: PASS.

- [ ] **Step 5: Wire the Ask tab in `App.tsx`**

Add the imports (biome orders them): `import {type AskView, askView} from './ask.js';` and `import {AskTab} from './ui/Ask.js';`.

Add after `useBackfill`:

```tsx
interface AskHost {
  view: AskView;
  error: string | null;
  start: (selection: {provider: string; model: string} | null) => void;
}

function useAsk(
  session: WorkspaceSession,
  state: WorkspaceState,
  ui: UiState,
  dispatch: Dispatch<UiAction>,
): AskHost {
  const {core, captured, asks} = state;
  const query = state.queries.chat_options;
  const options = query.response?.chat_options ?? null;
  // A failed query (or one sent while disconnected) is retried below, so it never reads as "no harness".
  const checking = query.loading || query.response === null;
  const view = useMemo(
    () =>
      askView({
        threads: core.chatThreads,
        transcripts: core.chatTranscripts,
        captured,
        asks,
        options,
        checking,
        selected: ui.thread,
      }),
    [core.chatThreads, core.chatTranscripts, captured, asks, options, checking, ui.thread],
  );
  // Options are asked for where they show (Ask, Notes, the palette), again as the run's status
  // moves (a run still starting reports none yet), and when the connection returns.
  const wanted = ui.pane === 'ask' || ui.pane === 'notes' || ui.palette;
  const offered = view.harness === 'available';
  const connected = state.connection === 'connected';
  useEffect(() => {
    if (wanted && !offered && connected && core.status !== 'connecting')
      void session.load('chat_options');
  }, [session, wanted, offered, connected, core.status]);
  const [failure, setFailure] = useState<{runId: string | null; message: string} | null>(null);
  // A thread error belongs to the run it happened in.
  const error = failure !== null && failure.runId === state.runId ? failure.message : null;
  const start = (selection: {provider: string; model: string} | null) => {
    const runId = state.runId;
    setFailure(null);
    dispatch({type: 'menu', menu: null});
    session.createThread(selection).then(
      id => dispatch({type: 'thread', id}),
      (reason: unknown) =>
        setFailure({
          runId,
          message: `Could not start a thread: ${reason instanceof Error ? reason.message : String(reason)}`,
        }),
    );
  };
  return {view, error, start};
}
```

Add `ask: AskHost;` to `PaneHostProps`.

Add after `PaneBody`:

```tsx
function askTab(props: PaneHostProps) {
  const {state, ui, dispatch, session, ask} = props;
  const connected = state.connection === 'connected';
  return (
    <AskTab
      view={ask.view}
      menu={ui.menu}
      draft={ui.drafts.ask}
      reason={connected ? null : 'Asking resumes when the connection returns'}
      error={ask.error}
      onMenu={menu => dispatch({type: 'menu', menu})}
      onThread={id => dispatch({type: 'thread', id})}
      onNewThread={ask.start}
      onDraft={text => dispatch({type: 'draft', target: 'ask', text})}
      onSend={text => Promise.resolve(session.ask(text, ask.view.current.id))}
    />
  );
}
```

In `PaneBody`, replace `return <Placeholder scope="Run" text="Chat about this run is not available yet." />;` with `return askTab(props);`.

In `App`, add after `const history = useBackfill(session, state, view.round);`:

```tsx
  const ask = useAsk(session, state, ui, dispatch);
```

and add `ask={ask}` to the `<RunPane …/>` element (after `onAgent={selectAgent}`).

- [ ] **Step 6: Teach the mocked gateway experiment chat**

In `e2e/gateway.ts`:

Add to `GatewayRequest`:

```ts
  thread_id?: string | null;
  provider?: string;
  model?: string;
  title?: string | null;
```

Add after `DEMO_CONTEXT`:

```ts
/** [mock] The run's chat offer: the run model, then one suggestion. */
const CHAT_OPTIONS = {
  providers: [
    {
      provider: 'claude',
      models: [
        {model: 'claude-opus-5', source: 'run', default: true},
        {model: 'claude-sonnet-5', source: 'suggested'},
      ],
    },
  ],
};
/** [mock] The recording has no chat agent behind it. */
export const MOCK_ANSWER =
  'Mock reply. This gateway replays a recorded run; there is no agent behind it.';
type ChatMode = 'on' | 'off' | 'late';
```

In `class DemoRun`, add the fields `threads = 0;`, `optionsAsked = 0;` and `chat: ChatMode = 'on';`, and add after `react`:

```ts
  /** Experiment chat as the backend answers it: recorded to the journal first, then returned. */
  chatAnswer(request: GatewayRequest): Record<string, unknown> | null {
    switch (request.type) {
      case 'query.chat_options': {
        this.optionsAsked += 1;
        const offered = this.chat === 'on' || (this.chat === 'late' && this.optionsAsked > 1);
        return offered ? {chat_options: CHAT_OPTIONS} : {};
      }
      case 'query.chat_thread_create':
        return this.createThread(request);
      case 'query.chat':
        return this.answerChat(request);
      default:
        return null;
    }
  }

  createThread(request: GatewayRequest): Record<string, unknown> {
    const spec = {
      thread_id: `thread-${++this.threads}`,
      title: request.title ?? '',
      driver: 'agentshim',
      provider: request.provider ?? 'claude',
      model: request.model ?? 'claude-opus-5',
    };
    this.push({
      type: 'chat_thread_created',
      agent_kind: 'chat',
      round_label: 'experiment-chat',
      chat_thread_id: spec.thread_id,
      data: {kind: 'chat_thread_created', ...spec, created_at: '2026-09-25T14:02:00Z'},
    });
    return {chat_thread: spec, events: [this.events.at(-1)]};
  }

  answerChat(request: GatewayRequest): Record<string, unknown> {
    const thread = request.thread_id ?? null;
    const first =
      thread !== null && !this.events.some(event => event.type === 'chat' && event.chat_thread_id === thread);
    this.push({
      type: 'chat',
      text: request.text ?? '',
      status: 'answered',
      agent_kind: 'chat',
      round_label: 'experiment-chat',
      chat_thread_id: thread,
      data: {
        kind: 'chat',
        answer: MOCK_ANSWER,
        thread_title: first ? (request.text ?? null) : null,
        invocation_id: `chat-${this.sequence + 1}`,
      },
    });
    return {
      chat: {question: request.text ?? '', answer: MOCK_ANSWER, thread_id: thread},
      events: [this.events.at(-1)],
    };
  }
```

In `onRequest`, replace `const fields = answer(request, this.events, snapshot);` with:

```ts
    const fields = this.chatAnswer(request) ?? answer(request, this.events, snapshot);
```

Change `mockGateway`'s options type to `options: {through?: number; status?: RunStatus; chat?: 'off' | 'late'} = {}` and add after `const run = new DemoRun(…);`:

```ts
  run.chat = options.chat ?? 'on';
```

- [ ] **Step 7: Add the e2e tests and screens**

In `e2e/app.spec.ts`, add `MOCK_ANSWER` to the `./gateway.js` import, and replace plan 3's test `'Ask and Notes say they are not available yet; Notes is also in the ••• menu'` with:

```ts
test('Notes opens from the ••• menu', async ({page}) => {
  await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Notes'}).click();
  await expect(page.getByRole('tab', {name: 'Notes'})).toHaveAttribute('aria-selected', 'true');
});

const askPane = async (page: Page) => {
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  const pane = page.getByRole('complementary', {name: 'Run details'});
  await pane.getByRole('tab', {name: 'Ask'}).click();
  return pane;
};

test('Ask: a question gets one answer; a model starts a thread; the switcher returns', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  const box = pane.getByRole('textbox', {name: 'Ask about this run'});
  await box.fill('Why did round 3 fail the judge?');
  await box.press('Enter');
  await expect(box).toHaveValue('');
  await expect(pane.locator('.human')).toHaveText(['Why did round 3 fail the judge?']);
  await expect(pane.locator('.answer')).toHaveCount(1);
  await expect(pane.locator('.answer')).toContainText(MOCK_ANSWER);
  await pane.getByRole('button', {name: 'Chat model'}).click();
  await page.getByRole('menuitemradio', {name: 'claude-sonnet-5'}).click();
  await expect(pane.getByRole('button', {name: 'Chat model'})).toContainText('claude-sonnet-5');
  await expect(pane.locator('.human')).toHaveCount(0);
  expect(
    gateway.requests.filter(request => request.type === 'query.chat_thread_create').map(request => request.model),
  ).toEqual(['claude-sonnet-5']);
  await pane.getByRole('button', {name: /^Thread:/}).click();
  await expect(page.getByRole('menuitemradio')).toHaveCount(2);
  await page.getByRole('menuitemradio', {name: /^Why did round 3 fail the judge\?/}).click();
  await expect(pane.locator('.human')).toHaveCount(1);
});

test('Ask without a chat harness says so and offers no composer', async ({page}) => {
  await mockGateway(page, {chat: 'off'});
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  await expect(pane).toContainText('This run offers no chat harness.');
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toHaveCount(0);
});

test('Ask offers chat once a starting run reports its options', async ({page}) => {
  const gateway = await mockGateway(page, {chat: 'late'});
  await page.goto('/?token=e2e');
  const pane = await askPane(page);
  await expect(pane).toContainText('This run offers no chat harness.');
  gateway.setStatus('pausing');
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toBeVisible();
});
```

(`Page` is already imported from `@playwright/test` in `app.spec.ts`.)

In `e2e/screens.spec.ts`: add `chat?: 'off';` to `interface Screen`; replace the `mockGateway(…)` call in the test body with:

```ts
      const gateway = await mockGateway(page, {
        ...(screen.through === undefined ? {} : {through: screen.through}),
        ...(screen.chat === undefined ? {} : {chat: screen.chat}),
      });
```

Add after the `runs` helper:

```ts
const openPane = async (page: Page, tab: string) => {
  await page.getByRole('button', {name: 'Toggle side pane'}).click();
  await page.getByRole('complementary', {name: 'Run details'}).getByRole('tab', {name: tab}).click();
};
const askQuestion = async (page: Page) => {
  await openPane(page, 'Ask');
  const box = page.getByRole('textbox', {name: 'Ask about this run'});
  await box.fill('Why did round 3 fail the judge?');
  await box.press('Enter');
  await page.getByText(/^Mock reply\./).waitFor();
};
```

Replace plan 3's `ask` entry in `SCREENS` with:

```ts
  {name: 'ask', act: askQuestion},
  {
    name: 'ask-thread',
    act: async page => {
      await askQuestion(page);
      await page.getByRole('button', {name: /^Thread:/}).click();
    },
  },
  {
    name: 'ask-model',
    act: async page => {
      await askQuestion(page);
      await page.getByRole('button', {name: 'Chat model'}).click();
    },
  },
  {name: 'ask-nochat', chat: 'off', act: async page => openPane(page, 'Ask')},
```

- [ ] **Step 8: Run everything**

Run: `pnpm --filter @vibesys/web test && pnpm --filter @vibesys/web check && pnpm exec biome check --write web && pnpm check:knip && pnpm --filter @vibesys/web test:e2e`
Expected: all PASS. If biome reports `Placeholder` unused in `App.tsx`'s import, leave it: Notes still uses it until Task 5.

- [ ] **Step 9: Capture and review**

Run: `CAPTURE_DIR="$TMPDIR/vs-web-5-t4" pnpm --filter @vibesys/web exec playwright test screens -g ask`
Open `ask-1440-{dark,light}.png` beside mockup `#ask` (scope `Run`, thread title with chevron, `+` at the right of the head row, the question as a right-aligned bubble, the model name above the answer, the composer pill with the model chip at the pane's foot), `ask-thread-*` beside `#thread` (300px popover under the title: `2 threads`, current row highlighted, counts at the right, a separator, `New thread`), `ask-model-*` beside `#model` (popover above the chip, `Claude Code harness` heading, check on the current model), `ask-nochat-*` beside `#nochat` (only the head row and the one line). Also open the 1024 frames: the pane keeps 340px and nothing overflows horizontally.

- [ ] **Step 10: Commit**

```bash
git add web/src/ui/Ask.tsx web/src/ui/Ask.test.tsx web/src/ui/TitleRow.tsx web/src/App.tsx web/src/window.css web/e2e
git commit -m "feat(web): Ask tab with threads, a model picker and the no-harness state" -m "<the session's attribution line>"
```

---

### Task 5: Notes tab

**Files:**
- Create: `clients/web/src/notes.ts`, `clients/web/src/ui/Notes.tsx`
- Modify: `clients/web/src/App.tsx`, `clients/web/src/main.tsx`, `clients/web/src/window.css`, `clients/web/e2e/gateway.ts`, `clients/web/e2e/app.spec.ts`, `clients/web/e2e/screens.spec.ts`; `clients/web/src/ui/Pane.tsx` and `Pane.test.tsx` only if `Placeholder` becomes unused
- Test: `clients/web/src/notes.test.ts`, `clients/web/src/ui/Notes.test.tsx`

**Interfaces:**
- Consumes: plan 2's `GET/PUT /api/notes/{run}` (`{note: {runId, text, createdAt, updatedAt} | null}`, errors `{error: {code, message}}`, `Authorization: Bearer <home token>`); `AskHost` (Task 4); `draft` action (Task 3); plan 3's `PaneHead`, `PaneBody`, `RunPane`.
- Produces:
  - `notes.ts`: `interface NoteRecord {runId: string; text: string; createdAt: string; updatedAt: string}`; `interface NotesApi {get(runId: string): Promise<NoteRecord | null>; put(runId: string, text: string, keepalive?: boolean): Promise<NoteRecord>}`; `httpNotesApi(token: string, fetcher?: typeof fetch): NotesApi`; `type NoteState = {phase: 'unavailable'} | {phase: 'loading'; runId: string} | {phase: 'failed'; runId: string; message: string} | {phase: 'ready'; runId: string; text: string; saved: string; error: string | null}`; `type NoteAction`; `noteReducer(state, action): NoteState`; `INITIAL_NOTE`; `serialSaver(api): (runId: string, text: string) => Promise<NoteRecord>`; `asDraft(text: string): string`; `interface NoteController {state: NoteState; edit(text: string): void; flush(): void; retry(): void}`; `useNote(api: NotesApi | null, runId: string | null, wanted: boolean): NoteController`
  - `ui/Notes.tsx`: `NotesTab({note, canSteer, harness, onEdit, onBlur, onRetry, onSteerDraft, onAskDraft})`, where `harness: AskView['harness']`
  - `App.tsx`: `AppProps.notes: NotesApi | null`; `PaneHostProps.note: NoteController`; `notesTab(props: PaneHostProps)`
  - `e2e/gateway.ts`: `mockNotes(page, text: string | null): Promise<{puts: string[]; auth: string[]}>`

- [ ] **Step 1: Write the failing tests**

`src/notes.test.ts`:

```ts
import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {
  asDraft,
  httpNotesApi,
  INITIAL_NOTE,
  type NoteState,
  type NotesApi,
  noteReducer,
  serialSaver,
} from './notes.js';

const RECORD = {runId: 'a/b c', text: 'check p99', createdAt: '2026-09-25T14:00:00Z', updatedAt: '2026-09-25T14:05:00Z'};

test('the notes client: bearer token, encoded run id, JSON body, keepalive on request', async () => {
  const calls: {url: string; init: RequestInit | undefined}[] = [];
  const fetcher = (async (url: string, init?: RequestInit) => {
    calls.push({url, init});
    return new Response(JSON.stringify({note: RECORD}), {status: 200});
  }) as typeof fetch;
  const api = httpNotesApi('tok', fetcher);
  assert.deepEqual(await api.get('a/b c'), RECORD);
  await api.put('a/b c', 'check p99', true);
  assert.equal(calls[0]?.url, '/api/notes/a%2Fb%20c');
  assert.deepEqual(calls[0]?.init?.headers, {Authorization: 'Bearer tok'});
  assert.equal(calls[1]?.init?.method, 'PUT');
  assert.equal(calls[1]?.init?.body, JSON.stringify({text: 'check p99'}));
  assert.equal(calls[1]?.init?.keepalive, true);
  assert.deepEqual(calls[1]?.init?.headers, {Authorization: 'Bearer tok', 'Content-Type': 'application/json'});
});

test('the notes client reports the server message, or the status when the body is not JSON', async () => {
  const typed = httpNotesApi('tok', (async () =>
    new Response(JSON.stringify({error: {code: 'invalid_request', message: 'run id too long'}}), {status: 400})) as typeof fetch);
  await assert.rejects(typed.get('x'), /run id too long/);
  const bare = httpNotesApi('tok', (async () => new Response('Not found', {status: 404})) as typeof fetch);
  await assert.rejects(bare.get('x'), /Notes are unavailable \(HTTP 404\)/);
  const empty = httpNotesApi('tok', (async () => new Response(JSON.stringify({note: null}), {status: 200})) as typeof fetch);
  assert.equal(await empty.get('x'), null);
});

test('a note loads, edits and saves; unsaved text is whatever differs from the last save', () => {
  let state = noteReducer(INITIAL_NOTE, {type: 'load', runId: 'r1'});
  state = noteReducer(state, {type: 'loaded', runId: 'r1', text: 'old'});
  state = noteReducer(state, {type: 'edit', text: 'new'});
  assert.deepEqual(state, {phase: 'ready', runId: 'r1', text: 'new', saved: 'old', error: null});
  state = noteReducer(state, {type: 'saveFailed', runId: 'r1', message: 'offline'});
  assert.equal(state.phase === 'ready' && state.error, 'offline');
  state = noteReducer(state, {type: 'saved', runId: 'r1', text: 'new'});
  assert.deepEqual(state, {phase: 'ready', runId: 'r1', text: 'new', saved: 'new', error: null});
});

test('a stale run\'s results are ignored', () => {
  const loading = noteReducer(INITIAL_NOTE, {type: 'load', runId: 'r2'});
  assert.equal(noteReducer(loading, {type: 'loaded', runId: 'r1', text: 'other run'}), loading);
  const ready: NoteState = {phase: 'ready', runId: 'r2', text: 'b', saved: 'a', error: null};
  assert.equal(noteReducer(ready, {type: 'saved', runId: 'r1', text: 'b'}), ready);
  assert.equal(noteReducer(ready, {type: 'saveFailed', runId: 'r1', message: 'x'}), ready);
});

test('nothing is editable before the note loads, or after it failed to load', () => {
  const loading = noteReducer(INITIAL_NOTE, {type: 'load', runId: 'r1'});
  assert.equal(noteReducer(loading, {type: 'edit', text: 'typed'}), loading);
  const failed = noteReducer(loading, {type: 'loadFailed', runId: 'r1', message: 'HTTP 500'});
  assert.deepEqual(failed, {phase: 'failed', runId: 'r1', message: 'HTTP 500'});
  assert.equal(noteReducer(failed, {type: 'edit', text: 'typed'}), failed);
});

test('saves go out one at a time, in the order they were made, past a failed one', async () => {
  const order: string[] = [];
  let release: () => void = () => {};
  const api: NotesApi = {
    get: async () => null,
    put: async (runId, text) => {
      if (text === 'first') await new Promise<void>(resolve => {
        release = resolve;
      });
      if (text === 'bad') throw new Error('offline');
      order.push(text);
      return {...RECORD, runId, text};
    },
  };
  const save = serialSaver(api);
  const first = save('r', 'first');
  const bad = save('r', 'bad');
  const last = save('r', 'last');
  await Promise.resolve();
  assert.deepEqual(order, []);
  release();
  await first;
  await assert.rejects(bad, /offline/);
  await last;
  assert.deepEqual(order, ['first', 'last']);
});

test('a note becomes a single-line draft', () => {
  assert.equal(asDraft('  Line one\n\n  line two \n'), 'Line one line two');
});
```

`src/ui/Notes.test.tsx`:

```tsx
import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {NoteState} from '../notes.js';
import {NotesTab, type NotesTabProps} from './Notes.js';

const READY: NoteState = {phase: 'ready', runId: 'r1', text: 'Check p99.', saved: 'Check p99.', error: null};
const props = (overrides: Partial<NotesTabProps> = {}): NotesTabProps => ({
  note: READY,
  canSteer: true,
  harness: 'available',
  onEdit: () => {},
  onBlur: () => {},
  onRetry: () => {},
  onSteerDraft: () => {},
  onAskDraft: () => {},
  ...overrides,
});

test('the editor with its scope line and both draft buttons', () => {
  const html = renderToStaticMarkup(<NotesTab {...props()} />);
  assert.match(html, /<span class="scope">Run<\/span><span>private to you, never sent<\/span>/);
  assert.match(html, /<textarea class="notesed" aria-label="Notes"[^>]*>Check p99\.<\/textarea>/);
  assert.match(html, /title="Put this note in the steer composer; nothing is sent">Use as steer draft</);
  assert.match(html, /title="Put this note in the Ask composer; nothing is sent">Use as ask draft</);
});

test('empty or ended: the draft buttons are off and say why on hover; a failed save is shown', () => {
  const empty = renderToStaticMarkup(<NotesTab {...props({note: {...READY, text: ' '}})} />);
  assert.equal(empty.match(/disabled=""/g)?.length, 2);
  const ended = renderToStaticMarkup(
    <NotesTab {...props({canSteer: false, harness: 'none', note: {...READY, error: 'offline'}})} />,
  );
  assert.match(ended, /disabled="" title="The run has ended">Use as steer draft/);
  assert.match(ended, /disabled="" title="This run offers no chat harness">Use as ask draft/);
  const checking = renderToStaticMarkup(<NotesTab {...props({harness: 'checking'})} />);
  assert.match(checking, /disabled="" title="Checking the chat harness…">Use as ask draft/);
  assert.match(ended, /role="alert">Not saved: offline</);
});

test('loading, failed with Retry, and no home server', () => {
  assert.match(renderToStaticMarkup(<NotesTab {...props({note: {phase: 'loading', runId: 'r1'}})} />), /Loading…/);
  const failed = renderToStaticMarkup(<NotesTab {...props({note: {phase: 'failed', runId: 'r1', message: 'HTTP 500'}})} />);
  assert.match(failed, /role="alert">Could not load the note: HTTP 500</);
  assert.match(failed, />Retry</);
  assert.doesNotMatch(failed, /textarea/);
  assert.match(renderToStaticMarkup(<NotesTab {...props({note: {phase: 'unavailable'}})} />), /Notes are kept by the VibeSys home server/);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm --filter @vibesys/web test`
Expected: FAIL: `./notes.js` and `./Notes.js` not found.

- [ ] **Step 3: Implement `src/notes.ts`**

```ts
/**
 * Run notes: private text per run, kept by the home server in the TUI's file
 * (`GET/PUT /api/notes/{run}`, last write wins). A note reaches an agent only when the user puts it
 * in a composer and sends it.
 */
import {type Dispatch, type RefObject, useCallback, useEffect, useMemo, useReducer, useRef} from 'react';

export interface NoteRecord {
  runId: string;
  text: string;
  createdAt: string;
  updatedAt: string;
}

export interface NotesApi {
  get(runId: string): Promise<NoteRecord | null>;
  /** `keepalive` lets the request outlive the page (the `pagehide` save). */
  put(runId: string, text: string, keepalive?: boolean): Promise<NoteRecord>;
}

const errorText = (error: unknown): string => (error instanceof Error ? error.message : String(error));

export function httpNotesApi(token: string, fetcher: typeof fetch = fetch): NotesApi {
  const call = async (runId: string, init: RequestInit = {}): Promise<NoteRecord | null> => {
    const writes = init.body !== undefined;
    const response = await fetcher(`/api/notes/${encodeURIComponent(runId)}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(writes ? {'Content-Type': 'application/json'} : {}),
      },
    });
    const body = (await response.json().catch(() => ({}))) as {
      note?: NoteRecord | null;
      error?: {message?: string};
    };
    if (!response.ok) {
      throw new Error(body.error?.message ?? `Notes are unavailable (HTTP ${response.status})`);
    }
    return body.note ?? null;
  };
  return {
    get: runId => call(runId),
    put: async (runId, text, keepalive = false) => {
      const note = await call(runId, {method: 'PUT', body: JSON.stringify({text}), keepalive});
      if (note === null) throw new Error('The home server returned no note.');
      return note;
    },
  };
}

export type NoteState =
  | {phase: 'unavailable'}
  | {phase: 'loading'; runId: string}
  | {phase: 'failed'; runId: string; message: string}
  /** `saved` is the text the server last confirmed; the note is unsaved while `text` differs. */
  | {phase: 'ready'; runId: string; text: string; saved: string; error: string | null};

export type NoteAction =
  | {type: 'load'; runId: string}
  | {type: 'loaded'; runId: string; text: string}
  | {type: 'loadFailed'; runId: string; message: string}
  | {type: 'edit'; text: string}
  | {type: 'saved'; runId: string; text: string}
  | {type: 'saveFailed'; runId: string; message: string};

export const INITIAL_NOTE: NoteState = {phase: 'loading', runId: ''};

/** Results for another run than the one shown are dropped; nothing edits a note that is not loaded. */
export function noteReducer(state: NoteState, action: NoteAction): NoteState {
  if (action.type === 'load') return {phase: 'loading', runId: action.runId};
  if (state.phase === 'unavailable' || state.phase === 'failed') return state;
  if (action.type === 'edit') return state.phase === 'ready' ? {...state, text: action.text} : state;
  if (action.runId !== state.runId) return state;
  switch (action.type) {
    case 'loaded':
      return {phase: 'ready', runId: action.runId, text: action.text, saved: action.text, error: null};
    case 'loadFailed':
      return {phase: 'failed', runId: action.runId, message: action.message};
    case 'saved':
      return state.phase === 'ready' ? {...state, saved: action.text, error: null} : state;
    case 'saveFailed':
      return state.phase === 'ready' ? {...state, error: action.message} : state;
  }
}

/** Saves one at a time, in call order, so a slow earlier save never lands after a later one. */
export function serialSaver(api: NotesApi): (runId: string, text: string) => Promise<NoteRecord> {
  let tail: Promise<unknown> = Promise.resolve();
  return (runId, text) => {
    const next = tail.then(() => api.put(runId, text));
    tail = next.catch(() => undefined);
    return next;
  };
}

/** Both composers are single-line inputs, which drop line breaks: fold them into spaces. */
export function asDraft(text: string): string {
  return text.trim().replace(/\s*\n\s*/g, ' ');
}

export interface NoteController {
  state: NoteState;
  edit: (text: string) => void;
  /** Saves unsaved text now (the editor lost focus). */
  flush: () => void;
  retry: () => void;
}

const SAVE_DELAY_MS = 500;
const UNAVAILABLE: NoteState = {phase: 'unavailable'};

function useFlush(
  api: NotesApi | null,
  latest: RefObject<NoteState>,
  dispatch: Dispatch<NoteAction>,
): (keepalive?: boolean) => void {
  const save = useMemo(() => (api === null ? null : serialSaver(api)), [api]);
  return useCallback(
    (keepalive = false) => {
      const note = latest.current;
      if (api === null || save === null || note.phase !== 'ready' || note.text === note.saved) return;
      const {runId, text} = note;
      // ponytail: the pagehide save skips the queue (the page is going away), so a slower queued save
      // can still land after it; a note over fetch's 64 KiB keepalive limit fails there.
      const put = keepalive ? api.put(runId, text, true) : save(runId, text);
      put.then(
        () => dispatch({type: 'saved', runId, text}),
        (error: unknown) => dispatch({type: 'saveFailed', runId, message: errorText(error)}),
      );
    },
    [api, save, latest, dispatch],
  );
}

/**
 * The note of the run on screen: loaded on its first view (`wanted`), saved 500 ms after typing
 * stops, on blur, before another run's note loads, and on `pagehide`.
 */
export function useNote(api: NotesApi | null, runId: string | null, wanted: boolean): NoteController {
  const [state, dispatch] = useReducer(noteReducer, INITIAL_NOTE);
  const latest = useRef(state);
  latest.current = state;
  const flush = useFlush(api, latest, dispatch);
  const load = useCallback(
    (run: string) => {
      if (api === null) return;
      dispatch({type: 'load', runId: run});
      api.get(run).then(
        note => dispatch({type: 'loaded', runId: run, text: note?.text ?? ''}),
        (error: unknown) => dispatch({type: 'loadFailed', runId: run, message: errorText(error)}),
      );
    },
    [api],
  );
  const target = api !== null && wanted && runId !== null ? runId : null;
  useEffect(() => {
    const note = latest.current;
    if (target === null || (note.phase !== 'unavailable' && note.phase !== 'failed' && note.runId === target)) return;
    flush();
    load(target);
  }, [target, flush, load]);
  const text = state.phase === 'ready' ? state.text : null;
  useEffect(() => {
    if (text === null) return;
    const timer = setTimeout(() => flush(), SAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [text, flush]);
  useEffect(() => {
    const onHide = () => flush(true);
    addEventListener('pagehide', onHide);
    return () => removeEventListener('pagehide', onHide);
  }, [flush]);
  return {
    state: api === null ? UNAVAILABLE : state,
    edit: next => dispatch({type: 'edit', text: next}),
    flush: () => flush(),
    retry: () => {
      if (target !== null) load(target);
    },
  };
}
```

- [ ] **Step 4: Implement `src/ui/Notes.tsx`**

```tsx
import type {AskView} from '../ask.js';
import type {NoteState} from '../notes.js';
import {PaneHead} from './Pane.js';

export interface NotesTabProps {
  note: NoteState;
  /** The steer composer is shown (the run has not ended). */
  canSteer: boolean;
  /** Ask has a composer only while the run offers a chat harness. */
  harness: AskView['harness'];
  onEdit: (text: string) => void;
  onBlur: () => void;
  onRetry: () => void;
  onSteerDraft: () => void;
  onAskDraft: () => void;
}

export function NotesTab(props: NotesTabProps) {
  return (
    <>
      <PaneHead scope="Run">
        <span>private to you, never sent</span>
      </PaneHead>
      <NoteBody {...props} />
    </>
  );
}

function NoteBody(props: NotesTabProps) {
  const {note} = props;
  switch (note.phase) {
    case 'unavailable':
      return <p className="empty1">Notes are kept by the VibeSys home server; open this run from the app to use them.</p>;
    case 'loading':
      return <p className="empty1">Loading…</p>;
    case 'failed':
      return (
        <div className="empty1">
          <p className="bad" role="alert">{`Could not load the note: ${note.message}`}</p>
          <button type="button" className="btn" onClick={props.onRetry}>
            Retry
          </button>
        </div>
      );
    case 'ready':
      return <Editor {...props} text={note.text} error={note.error} />;
  }
}

function Editor(props: NotesTabProps & {text: string; error: string | null}) {
  const {text, error, canSteer, harness} = props;
  const canAsk = harness === 'available';
  const empty = text.trim() === '';
  return (
    <>
      <textarea
        className="notesed"
        aria-label="Notes"
        placeholder="Write notes for this run…"
        value={text}
        onChange={event => props.onEdit(event.target.value)}
        onBlur={props.onBlur}
      />
      <div className="pfoot">
        <button
          type="button"
          className="btn"
          disabled={empty || !canSteer}
          title={canSteer ? 'Put this note in the steer composer; nothing is sent' : 'The run has ended'}
          onClick={props.onSteerDraft}
        >
          Use as steer draft
        </button>
        <button
          type="button"
          className="btn"
          disabled={empty || !canAsk}
          title={
            canAsk
              ? 'Put this note in the Ask composer; nothing is sent'
              : harness === 'checking'
                ? 'Checking the chat harness…'
                : 'This run offers no chat harness'
          }
          onClick={props.onAskDraft}
        >
          Use as ask draft
        </button>
        {error === null ? null : (
          <span className="hint bad" role="alert">{`Not saved: ${error}`}</span>
        )}
      </div>
    </>
  );
}
```

Append to `src/window.css`:

```css
/* notes */
.notesed { flex: 1; min-height: 200px; margin: 0; padding: 14px 16px; border: 0; outline: 0; resize: none; background: transparent; color: var(--text-1); font: 13px/20px var(--sans); }
.notesed::placeholder { color: var(--text-3); }
.pfoot { flex: none; display: flex; gap: 8px; padding: 10px 16px; border-top: 1px solid var(--border-subtle); align-items: center; }
.pfoot .hint { margin: 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.btn[disabled] { color: var(--text-3); background: none; }
.empty1 > p { margin: 0 0 8px; }
```

- [ ] **Step 5: Run the unit tests**

Run: `pnpm --filter @vibesys/web test`
Expected: PASS.

- [ ] **Step 6: Wire Notes in `App.tsx` and `main.tsx`**

In `src/App.tsx`, add the imports `import {asDraft, type NoteController, type NotesApi, useNote} from './notes.js';` and `import {NotesTab} from './ui/Notes.js';`.

Add `notes: NotesApi | null;` to `AppProps` with the doc comment `/** The home server's notes API; null when the page was opened without the home token. */`.

Add `note: NoteController;` to `PaneHostProps`.

Add after `askTab`:

```tsx
function notesTab(props: PaneHostProps) {
  const {view, dispatch, note, ask} = props;
  const draft = note.state.phase === 'ready' ? asDraft(note.state.text) : '';
  const focus = (id: string) => requestAnimationFrame(() => document.getElementById(id)?.focus());
  return (
    <NotesTab
      note={note.state}
      canSteer={!view.ended}
      harness={ask.view.harness}
      onEdit={note.edit}
      onBlur={note.flush}
      onRetry={note.retry}
      onSteerDraft={() => {
        dispatch({type: 'draft', target: 'steer', text: draft});
        focus('steer');
      }}
      onAskDraft={() => {
        dispatch({type: 'draft', target: 'ask', text: draft});
        dispatch({type: 'pane', pane: 'ask'});
        focus('ask');
      }}
    />
  );
}
```

In `PaneBody`, replace `return <Placeholder scope="Run" text="Run notes are not available yet." />;` with `return notesTab(props);`. Remove `Placeholder` from the `./ui/Pane.js` import if nothing else in `App.tsx` uses it.

In `App`, change the parameter list `{session, home}: AppProps` to `{session, home, notes}: AppProps`, add after `const ask = useAsk(session, state, ui, dispatch);`:

```tsx
  const note = useNote(notes, state.runId, ui.pane === 'notes');
```

and add `note={note}` to the `<RunPane …/>` element.

In `src/main.tsx`, add `import {httpNotesApi} from './notes.js';`, add before the `createRoot` line:

```tsx
// Notes live on the home server, which opened this page with its token; the replay has none.
const token = search.get('token');
const notes = token === null ? null : httpNotesApi(token);
```

and add `notes={notes}` to the `<App …/>` element.

Run `pnpm check:knip`. If it reports `Placeholder` as an unused export of `src/ui/Pane.tsx`, delete `Placeholder` from `Pane.tsx`, and in `Pane.test.tsx` drop it from the import, replace `<Placeholder scope="Run" text="Chat about this run is not available yet." />` with `<PaneHead scope="Run" />`, import `PaneHead` instead, and delete the assertion on `Chat about this run is not available yet\.`.

- [ ] **Step 7: Mock the notes endpoint and add the e2e tests and screens**

Append to `e2e/gateway.ts`:

```ts
/** [mock] The home server's notes endpoint (plan 2), last write wins. */
export async function mockNotes(
  page: Page,
  text: string | null,
): Promise<{puts: string[]; auth: string[]}> {
  const seen = {puts: [] as string[], auth: [] as string[]};
  let note = text;
  await page.route('**/api/notes/*', async route => {
    const request = route.request();
    seen.auth.push(request.headers()['authorization'] ?? '');
    const runId = decodeURIComponent(new URL(request.url()).pathname.split('/').at(-1) ?? '');
    if (request.method() === 'PUT') {
      note = (request.postDataJSON() as {text: string}).text;
      seen.puts.push(note);
    }
    const record =
      note === null
        ? null
        : {runId, text: note, createdAt: '2026-09-25T14:00:00Z', updatedAt: '2026-09-25T14:05:00Z'};
    await route.fulfill({json: {note: record}});
  });
  return seen;
}
```

Append to `e2e/app.spec.ts` (add `mockNotes` to the `./gateway.js` import):

```ts
const notesPane = async (page: Page) => {
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitem', {name: 'Notes'}).click();
  return page.getByRole('complementary', {name: 'Run details'});
};

test('Notes load, save as you type, and become a steer or ask draft without sending', async ({page}) => {
  const gateway = await mockGateway(page);
  const notes = await mockNotes(page, 'Check p99 before keeping round 7.');
  await page.goto('/?token=e2e');
  const pane = await notesPane(page);
  const editor = pane.getByRole('textbox', {name: 'Notes'});
  await expect(editor).toHaveValue('Check p99 before keeping round 7.');
  await editor.fill('Line one\nline two');
  await expect.poll(() => notes.puts.at(-1)).toBe('Line one\nline two');
  expect(notes.auth.every(value => value === 'Bearer e2e')).toBe(true);
  await pane.getByRole('button', {name: 'Use as steer draft'}).click();
  const steer = page.getByRole('textbox', {name: 'Steer the next agent call'});
  await expect(steer).toHaveValue('Line one line two');
  await expect(steer).toBeFocused();
  await pane.getByRole('button', {name: 'Use as ask draft'}).click();
  await expect(pane.getByRole('tab', {name: 'Ask'})).toHaveAttribute('aria-selected', 'true');
  await expect(pane.getByRole('textbox', {name: 'Ask about this run'})).toHaveValue('Line one line two');
  expect(gateway.requests.filter(request => request.type === 'command.steer' || request.type === 'query.chat')).toEqual([]);
});

test('Notes: edits survive a tab switch and are saved', async ({page}) => {
  await mockGateway(page);
  const notes = await mockNotes(page, null);
  await page.goto('/?token=e2e');
  const pane = await notesPane(page);
  const editor = pane.getByRole('textbox', {name: 'Notes'});
  await editor.fill('Typed, then away at once');
  await pane.getByRole('tab', {name: 'Changes'}).click();
  await expect.poll(() => notes.puts.at(-1)).toBe('Typed, then away at once');
  await pane.getByRole('tab', {name: 'Notes'}).click();
  await expect(editor).toHaveValue('Typed, then away at once');
});

test('Notes: a note that fails to load is not editable', async ({page}) => {
  await mockGateway(page);
  await page.route('**/api/notes/*', route => route.fulfill({status: 404, body: 'Not found'}));
  await page.goto('/?token=e2e');
  const pane = await notesPane(page);
  await expect(pane).toContainText('Could not load the note: Notes are unavailable (HTTP 404)');
  await expect(pane.getByRole('textbox', {name: 'Notes'})).toHaveCount(0);
  await expect(pane.getByRole('button', {name: 'Retry'})).toBeVisible();
});
```

In `e2e/screens.spec.ts`: add `mockNotes` to the `./gateway.js` import; add `notes?: 'fail';` to `interface Screen`; add after `const OUT = …`:

```ts
/** [mock] The mockup's note text. */
const NOTE =
  "Round 4 traded peak throughput for the buffer pool that round 5 needed. Check p99 latency before keeping round 7's admission delay.";
```

In the test body, before `await page.goto('/?token=e2e');`, add:

```ts
      if (screen.notes === 'fail')
        await page.route('**/api/notes/*', route => route.fulfill({status: 404, body: 'Not found'}));
      else await mockNotes(page, NOTE);
```

Add to `SCREENS`:

```ts
  {name: 'notes', act: async page => openPane(page, 'Notes')},
  {name: 'notes-failed', notes: 'fail', act: async page => openPane(page, 'Notes')},
```

- [ ] **Step 8: Run everything**

Run: `pnpm --filter @vibesys/web test && pnpm --filter @vibesys/web check && pnpm exec biome check --write web && pnpm check:knip && pnpm check:ts-architecture && pnpm --filter @vibesys/web test:e2e`
Expected: all PASS.

- [ ] **Step 9: Capture and review**

Run: `CAPTURE_DIR="$TMPDIR/vs-web-5-t5" pnpm --filter @vibesys/web exec playwright test screens -g notes`
Open `notes-1440-{dark,light}.png` beside mockup `#notes` (head row `Run  private to you, never sent`, the note filling the pane with no visible field border, the footer with the two buttons, hairline above it) and `notes-failed-*` (the error line in `--error`, Retry, no editor). Check the disabled button text stays readable (`--text-3`) in both themes.

- [ ] **Step 10: Commit**

```bash
git add web/src/notes.ts web/src/notes.test.ts web/src/ui/Notes.tsx web/src/ui/Notes.test.tsx web/src/ui/Pane.tsx web/src/ui/Pane.test.tsx web/src/App.tsx web/src/main.tsx web/src/window.css web/e2e
git commit -m "feat(web): run notes shared with the TUI, usable as steer or ask drafts" -m "<the session's attribution line>"
```

---

### Task 6: Theme switcher

**Files:**
- Create: `clients/web/src/theme.ts`
- Modify: `clients/web/src/ui/TitleRow.tsx`, `clients/web/src/App.tsx`, `clients/web/src/main.tsx`, `clients/web/e2e/app.spec.ts`, `clients/web/e2e/screens.spec.ts`
- Test: `clients/web/src/theme.test.ts`, `clients/web/src/ui/TitleRow.test.tsx`

**Interfaces:**
- Consumes: plan 3's `MoreMenu` (children placed first), `RunHeader`, `theme.css` (`:root[data-theme='light'|'dark']` fixes `color-scheme`).
- Produces:
  - `theme.ts`: `type ThemeChoice = 'system' | 'light' | 'dark'`; `THEMES: readonly ThemeChoice[]`; `THEME_LABELS: Readonly<Record<ThemeChoice, string>>`; `initialTheme(query: string | null, saved: string | null): ThemeChoice`; `applyTheme(root: Pick<Element, 'setAttribute' | 'removeAttribute'>, choice: ThemeChoice): void`; `savedTheme(): string | null`; `saveTheme(choice: ThemeChoice): void`
  - `ui/TitleRow.tsx`: `ThemeItems({theme, onTheme})` (a `Theme` heading and three `menuitemradio`s)
  - `App.tsx`: `AppProps.theme: ThemeChoice` (the theme the page opened with), `useTheme(opened): [ThemeChoice, (choice: ThemeChoice) => void]`, `RunHeader` gains `theme` and `onTheme`

- [ ] **Step 1: Write the failing tests**

`src/theme.test.ts`:

```ts
import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {applyTheme, initialTheme} from './theme.js';

test('?theme= wins, then the saved choice, then System; unknown values are ignored', () => {
  assert.equal(initialTheme('dark', 'light'), 'dark');
  assert.equal(initialTheme(null, 'light'), 'light');
  assert.equal(initialTheme('sepia', 'nope'), 'system');
  assert.equal(initialTheme(null, null), 'system');
});

test('Light and Dark set data-theme; System removes it so the OS decides', () => {
  const attributes = new Map<string, string>();
  const root = {
    setAttribute: (name: string, value: string) => void attributes.set(name, value),
    removeAttribute: (name: string) => void attributes.delete(name),
  };
  applyTheme(root, 'dark');
  assert.equal(attributes.get('data-theme'), 'dark');
  applyTheme(root, 'system');
  assert.equal(attributes.has('data-theme'), false);
});
```

Append to `src/ui/TitleRow.test.tsx` (add `ThemeItems` to the `./TitleRow.js` import):

```tsx
test('the theme items: one radio per choice, the current one checked', () => {
  const html = renderToStaticMarkup(<ThemeItems theme="light" onTheme={() => {}} />);
  assert.match(html, /<div class="gh">Theme<\/div>/);
  assert.equal(html.match(/role="menuitemradio"/g)?.length, 3);
  assert.match(html, /aria-checked="true" class="it">Light/);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm --filter @vibesys/web test`
Expected: FAIL: `./theme.js` not found; `ThemeItems` is not exported.

- [ ] **Step 3: Implement `src/theme.ts` and `ThemeItems`**

`src/theme.ts`:

```ts
/** Appearance: System follows the OS through light-dark(); Light and Dark set data-theme on <html>. */
export type ThemeChoice = 'system' | 'light' | 'dark';

export const THEMES: readonly ThemeChoice[] = ['system', 'light', 'dark'];
export const THEME_LABELS: Readonly<Record<ThemeChoice, string>> = {
  system: 'System',
  light: 'Light',
  dark: 'Dark',
};

const KEY = 'vibesys.theme';

const parseTheme = (value: string | null): ThemeChoice | null =>
  THEMES.find(theme => theme === value) ?? null;

/** `?theme=` wins (reviews and captures), then the saved choice, then System. */
export function initialTheme(query: string | null, saved: string | null): ThemeChoice {
  return parseTheme(query) ?? parseTheme(saved) ?? 'system';
}

export function applyTheme(
  root: Pick<Element, 'setAttribute' | 'removeAttribute'>,
  choice: ThemeChoice,
): void {
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
}

/** Storage can be unavailable (private windows, blocked site data); the choice then lasts the page. */
export function savedTheme(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function saveTheme(choice: ThemeChoice): void {
  try {
    localStorage.setItem(KEY, choice);
  } catch {
    // The choice still applies until the page reloads.
  }
}
```

In `src/ui/TitleRow.tsx`: add `Check` to the `lucide-react` import, add `import {THEME_LABELS, THEMES, type ThemeChoice} from '../theme.js';`, change the ••• button's `title="Copy run ID, stop the run"` to `title="Notes, theme, copy run ID, stop the run"` (update any `TitleRow.test.tsx` assertion on the old hint), and append:

```tsx
export function ThemeItems({theme, onTheme}: {theme: ThemeChoice; onTheme: (choice: ThemeChoice) => void}) {
  return (
    <>
      <div className="gh">Theme</div>
      {THEMES.map(choice => (
        <button
          key={choice}
          type="button"
          role="menuitemradio"
          aria-checked={choice === theme}
          className="it"
          onClick={() => onTheme(choice)}
        >
          {THEME_LABELS[choice]}
          {choice === theme ? <Check size={14} strokeWidth={1.5} className="d" aria-hidden /> : null}
        </button>
      ))}
    </>
  );
}
```

- [ ] **Step 4: Wire the theme**

In `src/App.tsx`: add `import {applyTheme, saveTheme, type ThemeChoice} from './theme.js';` and `ThemeItems` to the `./ui/TitleRow.js` import. Add `theme: ThemeChoice;` to `AppProps` with the doc comment `/** The theme the page opened with (see main.tsx). */`.

Add after `useWindowWidth`:

```tsx
function useTheme(opened: ThemeChoice): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [theme, setTheme] = useState(opened);
  const choose = (choice: ThemeChoice) => {
    setTheme(choice);
    applyTheme(document.documentElement, choice);
    saveTheme(choice);
    // A choice made here outranks the page's ?theme= from now on, reloads included.
    const url = new URL(location.href);
    if (url.searchParams.has('theme')) {
      url.searchParams.delete('theme');
      history.replaceState(history.state, '', url);
    }
  };
  return [theme, choose];
}
```

Replace the `RunHeader` signature line `function RunHeader(props: SectionProps & {sidebarShown: boolean; onShowSidebar: () => void}) {` with:

```tsx
function RunHeader(
  props: SectionProps & {
    sidebarShown: boolean;
    onShowSidebar: () => void;
    theme: ThemeChoice;
    onTheme: (choice: ThemeChoice) => void;
  },
) {
```

In `RunHeader`'s `<MoreMenu …>` children, after the `Notes` menu item's closing `</button>` and before `</MoreMenu>`, add:

```tsx
        <div className="sepl" />
        <ThemeItems
          theme={props.theme}
          onTheme={choice => {
            props.onTheme(choice);
            dispatch({type: 'menu', menu: null});
          }}
        />
        <div className="sepl" />
```

In `App`: change the parameter list to `{session, home, notes, theme: opened}: AppProps`, add `const [theme, chooseTheme] = useTheme(opened);` after `const note = useNote(…);`, and add `theme={theme} onTheme={chooseTheme}` to the `<RunHeader …/>` element.

In `src/main.tsx`: add `import {applyTheme, initialTheme, savedTheme} from './theme.js';`, replace

```ts
// System follows the OS through light-dark(); ?theme=light|dark forces one (reviews, captures).
const theme = search.get('theme');
if (theme === 'light' || theme === 'dark') document.documentElement.dataset['theme'] = theme;
```

with

```ts
// System follows the OS through light-dark(); ?theme=light|dark forces one (reviews, captures).
const theme = initialTheme(search.get('theme'), savedTheme());
applyTheme(document.documentElement, theme);
```

and add `theme={theme}` to the `<App …/>` element.

- [ ] **Step 5: Add the e2e test and screen**

Append to `e2e/app.spec.ts`:

```ts
test('the ••• menu switches the theme and the choice survives a reload', async ({page}) => {
  await mockGateway(page);
  await page.emulateMedia({colorScheme: 'dark'});
  await page.goto('/?token=e2e');
  const background = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.getByRole('button', {name: 'More'}).click();
  await expect(page.getByRole('menuitemradio', {name: 'System'})).toHaveAttribute('aria-checked', 'true');
  await page.getByRole('menuitemradio', {name: 'Light'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  expect(await background()).toBe('rgb(252, 252, 253)');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', {name: 'More'}).click();
  await page.getByRole('menuitemradio', {name: 'System'}).click();
  await expect(page.locator('html')).not.toHaveAttribute('data-theme', /.+/);
  expect(await background()).toBe('rgb(17, 17, 19)');
});
```

Add to `SCREENS` in `e2e/screens.spec.ts`:

```ts
  {name: 'theme-menu', act: async page => page.getByRole('button', {name: 'More'}).click()},
```

- [ ] **Step 6: Run everything**

Run: `pnpm --filter @vibesys/web test && pnpm --filter @vibesys/web check && pnpm exec biome check --write web && pnpm check:knip && pnpm --filter @vibesys/web test:e2e`
Expected: all PASS.

- [ ] **Step 7: Capture and review**

Run: `CAPTURE_DIR="$TMPDIR/vs-web-5-t6" pnpm --filter @vibesys/web exec playwright test screens -g theme-menu`
Open `theme-menu-1440-{dark,light}.png`: the ••• popover reads Notes, a hairline, `Theme` heading, System with a check, Light, Dark, a hairline, Copy run ID, a hairline, Stop run… in `--error`; compare its surface and radius with mockup `#stop`'s popover.

- [ ] **Step 8: Commit**

```bash
git add web/src/theme.ts web/src/theme.test.ts web/src/ui/TitleRow.tsx web/src/ui/TitleRow.test.tsx web/src/App.tsx web/src/main.tsx web/e2e
git commit -m "feat(web): System, Light and Dark theme switcher in the ••• menu" -m "<the session's attribution line>"
```

---

### Task 7: Palette: Ask and Appearance commands

**Files:**
- Modify: `clients/web/src/palette.ts`, `clients/web/src/ui/Palette.tsx`, `clients/web/src/App.tsx`, `clients/web/e2e/app.spec.ts`, `clients/web/e2e/screens.spec.ts`
- Test: `clients/web/src/palette.test.ts`

**Interfaces:**
- Consumes: plan 3's `Intent`, `PaletteItem`, `PaletteInput`, `paletteItems`, `runIntent`, `IntentContext`, `paletteInput`; `AskHost` (Task 4); `ThemeChoice`, `THEMES`, `THEME_LABELS`, `useTheme` (Task 6).
- Produces:
  - `palette.ts`: `Intent` gains `{kind: 'newThread'} | {kind: 'askMenu'; menu: 'thread' | 'model'} | {kind: 'theme'; theme: ThemeChoice}`; `PaletteItem.group` gains `'Ask' | 'Appearance'`; `PaletteInput` gains `ask: {threads: number; model: string | null} | null` (null without a chat harness) and `theme: ThemeChoice`
  - `App.tsx`: `IntentContext` gains `newThread: () => void` and `chooseTheme: (choice: ThemeChoice) => void`; `paletteInput(state, view, ui, sidebarShown, ask: AskView, theme: ThemeChoice)`

Each new item mirrors a visible control: New thread (`+` in Ask's head), Switch thread… (the thread title), Chat model… (the model chip), Theme (••• menu).

- [ ] **Step 1: Write the failing tests**

In `src/palette.test.ts`: add `ask: null,` and `theme: 'system',` to `BASE`; append these three entries to the expected array of `'the palette mirrors the visible controls'`, after `'Agent: Show the todos',`:

```ts
    'Appearance: Theme: System',
    'Appearance: Theme: Light',
    'Appearance: Theme: Dark',
```

and append:

```ts
test('Ask commands appear with a chat harness; the current theme is marked', () => {
  const items = paletteItems({...BASE, ask: {threads: 2, model: 'claude-opus-5'}, theme: 'dark'});
  assert.deepEqual(
    items.filter(entry => entry.group === 'Ask').map(entry => `${entry.label} ${entry.detail}`.trim()),
    ['New thread', 'Switch thread… 2 threads', 'Chat model… claude-opus-5'],
  );
  assert.deepEqual(
    items.filter(entry => entry.detail === 'current').map(entry => entry.label),
    ['Theme: Dark'],
  );
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm --filter @vibesys/web test`
Expected: FAIL: the Appearance items are missing; `ask` and `theme` are not in `PaletteInput`.

- [ ] **Step 3: Implement the palette additions**

In `src/palette.ts`:

Add `import {THEME_LABELS, THEMES, type ThemeChoice} from './theme.js';`.

Add to `Intent`:

```ts
  | {kind: 'newThread'}
  | {kind: 'askMenu'; menu: 'thread' | 'model'}
  | {kind: 'theme'; theme: ThemeChoice}
```

Change `group: 'Run' | 'Go to' | 'Agent';` to `group: 'Run' | 'Go to' | 'Agent' | 'Ask' | 'Appearance';`.

Add to `PaletteInput`:

```ts
  /** Ask's thread count and model; null while the run offers no chat harness (Ask shows no such controls). */
  ask: {threads: number; model: string | null} | null;
  theme: ThemeChoice;
```

Add before `paletteItems`:

```ts
function askItems(input: PaletteInput): PaletteItem[] {
  if (input.ask === null) return [];
  const {threads, model} = input.ask;
  return [
    item('ask-new', 'Ask', 'New thread', '', {kind: 'newThread'}),
    item('ask-thread', 'Ask', 'Switch thread…', threads === 1 ? '1 thread' : `${threads} threads`, {
      kind: 'askMenu',
      menu: 'thread',
    }),
    item('ask-model', 'Ask', 'Chat model…', model ?? '', {kind: 'askMenu', menu: 'model'}),
  ];
}

function themeItems(input: PaletteInput): PaletteItem[] {
  return THEMES.map(theme =>
    item(`theme-${theme}`, 'Appearance', `Theme: ${THEME_LABELS[theme]}`, theme === input.theme ? 'current' : '', {
      kind: 'theme',
      theme,
    }),
  );
}
```

Replace the body of `paletteItems` with:

```ts
  return [
    ...runItems(input),
    ...goToItems(input),
    ...agentItems(input),
    ...askItems(input),
    ...themeItems(input),
  ];
```

In `src/ui/Palette.tsx`, replace `const GROUPS: ReadonlyArray<PaletteItem['group']> = ['Run', 'Go to', 'Agent'];` with:

```tsx
const GROUPS: ReadonlyArray<PaletteItem['group']> = ['Run', 'Go to', 'Agent', 'Ask', 'Appearance'];
```

- [ ] **Step 4: Wire the intents in `App.tsx`**

Add `newThread: () => void;` and `chooseTheme: (choice: ThemeChoice) => void;` to `IntentContext`.

In `runIntent`, add these cases after `case 'reveal': …`:

```tsx
    case 'newThread':
      dispatch({type: 'pane', pane: 'ask'});
      context.newThread();
      return;
    case 'askMenu':
      dispatch({type: 'pane', pane: 'ask'});
      dispatch({type: 'menu', menu: intent.menu});
      return;
    case 'theme':
      context.chooseTheme(intent.theme);
      return;
```

Replace the `paletteInput` signature and its returned object's end so it reads:

```tsx
/** Palette items from what the window shows now: the agent filter's turns only, so none is a no-op. */
function paletteInput(
  state: WorkspaceState,
  view: View,
  ui: UiState,
  sidebarShown: boolean,
  ask: AskView,
  theme: ThemeChoice,
): PaletteInput {
```

and add to the returned object, after `todos: …,`:

```tsx
    ask:
      ask.harness === 'available'
        ? {threads: ask.threads.length, model: ask.current.model}
        : null,
    theme,
```

In `App`'s `<Palette …/>` element, replace `items={paletteItems(paletteInput(state, view, ui, layout.sidebar))}` with `items={paletteItems(paletteInput(state, view, ui, layout.sidebar, ask.view, theme))}` and replace `onRun={entry => runIntent(entry.intent, {dispatch, session, state, toggleSidebar})}` with:

```tsx
          onRun={entry =>
            runIntent(entry.intent, {
              dispatch,
              session,
              state,
              toggleSidebar,
              newThread: () => ask.start(null),
              chooseTheme,
            })
          }
```

- [ ] **Step 5: Add the e2e test and screens**

Append to `e2e/app.spec.ts`:

```ts
test('the palette reaches Ask\'s model menu, a new thread and the theme', async ({page}) => {
  const gateway = await mockGateway(page);
  await page.goto('/?token=e2e');
  await page.locator('.titlebar').click();
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('chat model');
  await expect(page.getByRole('option', {name: /Chat model…/})).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('tab', {name: 'Ask'})).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('menu', {name: 'Chat model'})).toBeVisible();
  await page.keyboard.press('Escape');
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('new thread');
  await page.keyboard.press('Enter');
  await expect
    .poll(() => gateway.requests.filter(request => request.type === 'query.chat_thread_create').length)
    .toBe(1);
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('theme: dark');
  await page.keyboard.press('Enter');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});
```

Add to `SCREENS` in `e2e/screens.spec.ts`:

```ts
  {
    name: 'palette-ask',
    act: async page => {
      await page.getByRole('button', {name: /Search and commands/}).click();
      await page.keyboard.type('thread');
      await page.getByRole('option', {name: /Switch thread…/}).waitFor();
    },
  },
  {
    name: 'palette-theme',
    act: async page => {
      await page.getByRole('button', {name: /Search and commands/}).click();
      await page.keyboard.type('theme');
    },
  },
```

- [ ] **Step 6: Run everything**

Run: `pnpm --filter @vibesys/web test && pnpm --filter @vibesys/web check && pnpm exec biome check --write web && pnpm check:knip && pnpm --filter @vibesys/web test:e2e`
Expected: all PASS.

- [ ] **Step 7: Capture and review**

Run: `CAPTURE_DIR="$TMPDIR/vs-web-5-t7" pnpm --filter @vibesys/web exec playwright test screens -g palette`
Compare `palette-ask-*` and `palette-theme-*` with mockup `#palette` (group headings `Ask` and `Appearance`, details in `--text-2`, `current` on the active theme, the result count in the footer), and the unfiltered `palette-*` for group order Run, Go to, Agent, Ask, Appearance.

- [ ] **Step 8: Commit**

```bash
git add web/src/palette.ts web/src/palette.test.ts web/src/ui/Palette.tsx web/src/App.tsx web/e2e
git commit -m "feat(web): Ask and theme commands in the ⌘K palette" -m "<the session's attribution line>"
```

---

### Task 8: Docs, full gates, and the review set

**Files:**
- Modify: `clients/web/README.md`

**Interfaces:** none new.

- [ ] **Step 1: Update the README's behaviour section**

In the `## Behavior` section of `clients/web/README.md`: delete the sentence `Ask and Notes are placeholders until sub-project 5.` wherever it stands; replace the bullet that begins `- Themes:` with the last bullet below; add the other two bullets after the transcript bullet:

```markdown
- Ask talks to the run's experiment chat: a thread switcher, New thread, and a model chip whose
  choices come from the run's `query.chat_options` (a model belongs to a thread, so choosing one
  starts a thread on it). One question per thread is in flight at a time. Without a chat harness
  the tab says so and keeps recorded threads readable.
- Notes are private run notes shared with the TUI through the home server
  (`GET/PUT /api/notes/{run}`, last write wins), saved as you type. "Use as steer draft" and
  "Use as ask draft" only fill a composer; nothing is sent.
- Themes: System (default), Light and Dark, from the ••• menu or ⌘K, remembered per browser;
  `?theme=light|dark` overrides for reviews and captures.
```

Run: `uv run python scripts/check_doc_links.py` (from the repository root)
Expected: no broken links.

- [ ] **Step 2: Run every gate from a clean build**

```bash
pnpm --filter @vibesys/web test
pnpm --filter @vibesys/web check
pnpm check:ts
pnpm check:ts-architecture
pnpm test:ts-architecture
pnpm check:knip
pnpm check:web-browser-bundle
pnpm --filter @vibesys/web build
pnpm --filter @vibesys/web test:e2e
```

Expected: all PASS. `rg -n "—" web/src web/e2e` prints nothing.

- [ ] **Step 3: Capture every new surface and review it**

Run: `CAPTURE_DIR="$TMPDIR/vs-web-5-final" pnpm --filter @vibesys/web exec playwright test screens`
Open the 1440 dark and 1440 light frame of every screen this plan added and name the mockup screen each matches, listing deviations:

| Screen | Mockup |
|---|---|
| `ask` | `#ask` |
| `ask-thread` | `#thread` |
| `ask-model` | `#model` |
| `ask-nochat` | `#nochat` |
| `notes` | `#notes` |
| `notes-failed` | none (error state; check tokens and copy) |
| `theme-menu` | `#stop` popover styling |
| `palette-ask`, `palette-theme` | `#palette` |

For each frame check: one Indigo accent only (focus ring, the Send button when ready, links); surfaces step by lightness (pane `--bg-app`, popovers and palette `--bg-raised`); no text fainter than `--text-3`; every visible string states one thing and the rest sits in hover titles. Glance at the 1024 frames for overflow. Then ask Codex (read-only) to critique the set against `mockup.html` and the spec's section 5; fix what it finds that the mockup supports, recapture, and keep `ask-1440-dark.png` and `notes-1440-dark.png` from Task 4, 5 and from here as the before/after pairs for the PR body.

- [ ] **Step 4: Commit**

```bash
git add web/README.md
git commit -m "docs(web): describe Ask, Notes and the theme switcher" -m "<the session's attribution line>"
```

---

## Self-Review

**Spec coverage (section 5 and the constraints it inherits):**

| Spec item | Task |
|---|---|
| Ask tab: thread switcher | 2 (`threads`, titles), 4 (`ThreadHead`, `Threads` popover) |
| New thread | 1 (`createThread`), 4 (`+` and menu item), 7 (palette) |
| Model picker from `query.chat_options` | 1 (on-demand query), 2 (`groups`, run default), 4 (chip and `Chat model` menu) |
| The no-chat-harness state | 2 (`harness`), 4 (tab copy, e2e `off` and `late`) |
| Notes editor | 5 (`notes.ts`, `NotesTab`, plan 2 endpoint) |
| "Use as steer draft", "Use as ask draft" (drafts only) | 3 (controlled composers, drafts), 5 (buttons, e2e asserts nothing sent) |
| Theme switcher (System, Light, Dark) | 6 (`theme.ts`, ••• menu), 7 (palette) |
| ⌘K mirrors visible controls; nothing only in it | 7 (each item names its visible control) |
| Hints on hover, one message per element | titles on the thread button, chip, draft buttons, Send; Task 8 review |
| No controls the protocol lacks | Global Constraints; Ask and Notes send only `query.chat*` and nothing on drafts |
| Tests: bun component tests, Playwright e2e on the demo, screenshots reviewed | every task; 8 (1440 dark and light per new surface, Codex review) |

**Placeholder scan:** every code step carries its code; conditional steps (removing `Placeholder`, updating an assertion on the old ••• hint) name the exact symbol and edit.

**Type consistency:** `SentAsk` (Task 1) feeds `AskInput.asks` (Task 2); `AskView`, `ThreadRow`, `ModelGroup`, `AskMessage` (Task 2) are what `ui/Ask.tsx` (Task 4) renders; `Menu` gains `thread` and `model` in Task 3 before Task 4 uses them; `UiState.thread` and `drafts` (Task 3) are read by `askTab`, `notesTab` and `RunComposer`; `AskHost` (Task 4) is read by `notesTab` (Task 5) and the palette wiring (Task 7); `NoteController` (Task 5) is `PaneHostProps.note`; `ThemeChoice`, `THEMES`, `THEME_LABELS` (Task 6) are used by `ThemeItems` (Task 6) and `palette.ts` (Task 7); `session.ask` returns `boolean`, wrapped in `Promise.resolve` for `Composer.onSend`.

**Remaining risks:** the Ask history shows only chat events inside the loaded window of a tail bootstrap (older questions appear once a round's backfill loads the events around them); the `pagehide` save bypasses the save queue and fetch's 64 KiB keepalive limit (marked `ponytail:` in `notes.ts`); the "unavailable" Notes state (no home token) is covered by the component test only, since the e2e harness always opens the page with a token.
