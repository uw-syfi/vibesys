/**
 * The run chat's side of the fake run server: it acknowledges every `command.control` (and keeps it
 * in `fakeHost.controls`, where a test reads the control id the page minted), creates a chat thread
 * on `query.chat_thread_create`, and answers `query.chat`. A test streams the events that follow
 * (an answer, a delivery step) with `fakeHost.emit`. `installFakeChat` runs in the page after
 * `installFakeFixtureServer`, so it is self-contained.
 */
import type {FakeProvides} from './fake-bridge.js';

export interface FakeChatSeed {
  readonly runId: string;
  /** The sequence a created thread's event takes: past every event of the run. */
  readonly firstSequence: number;
}

interface ChatRequest {
  readonly type: string;
  readonly control?: {readonly control_id?: string; readonly action?: string};
  readonly harness?: {readonly provider: string; readonly model: string};
  readonly text?: string;
  readonly thread_id?: string;
}

/** What `installFakeChat` provides on the welcome bridge: attaching the reader of an ended run. */
export type ChatMethods = 'attachReader';

export function installFakeChat(seed: FakeChatSeed): void {
  let readerAttaches = 0;
  const controls: {control_id?: string; action?: string}[] = [];
  const timestamp = '2026-10-15T16:31:00Z';
  const answerMore = (request: ChatRequest, envelope: object): readonly object[] | undefined => {
    if (request.type === 'command.control') {
      const control = request.control ?? {};
      controls.push(control);
      return [{...envelope, ok: true, ack: {action: control.action, status: 'pending'}}];
    }
    if (request.type === 'query.chat_thread_create') {
      const harness = request.harness ?? {provider: 'claude', model: 'opus'};
      const thread = {thread_id: 'thread-new', title: '', ...harness};
      const created = {
        type: 'chat_thread_created',
        sequence: seed.firstSequence,
        run_id: seed.runId,
        timestamp,
        agent_kind: 'chat',
        chat_thread_id: thread.thread_id,
        data: {kind: 'chat_thread_created', created_at: timestamp, ...thread},
      };
      return [{...envelope, ok: true, chat_thread: thread, events: [created]}];
    }
    if (request.type === 'query.chat') {
      const chat = {question: request.text, answer: 'Done.', thread_id: request.thread_id};
      return [{...envelope, ok: true, chat}];
    }
    return undefined;
  };
  const page = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(page.vibesysWelcome, {
    attachReader: () => {
      readerAttaches += 1;
      return Promise.resolve({ok: true, value: {kind: 'attached'}});
    },
  } satisfies FakeProvides<ChatMethods>);
  // `readerAttaches` says how many times the page attached the reader of an ended run.
  Object.assign(page.fakeHost, {controls, answerMore, readerAttaches: () => readerAttaches});
}
