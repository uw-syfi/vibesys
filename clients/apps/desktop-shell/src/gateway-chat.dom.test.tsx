/**
 * The chat over the gateway, composed as the app composes it: the panel, the thread discovery, the
 * session tails and the steer cards over one fake peer that answers calls in any order.
 */
import {afterAll, beforeAll, expect, test} from 'bun:test';
import {create, fromBinary, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {ChatPanel} from '@vibesys/feature-run-chat/renderer';
import {HARNESS_LIST_UNREAD, NO_HARNESS_OFFERED} from '@vibesys/feature-run-chat/state';
import type {HostKey} from '@vibesys/hosts';
import {FakeGatewayBridge} from '@vibesys/ipc/testing';
import {
  ChatRole,
  ConfigurationFailedSchema,
  GetBlobsResponseSchema,
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  ListChatSessionsResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
  RecordSchema,
  type SteerMode,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {initialCoreState} from '@vibesys/run-state';
import {CHAT_CATALOG_METHOD, CHAT_SESSIONS_METHOD} from '@vibesys/runs/chat';
import {HOST_CALL_METHOD, IntentRefused} from '@vibesys/runs/intents';
import {SEEDS, Seeded} from '@vibesys/store/testing';

/** Each seed renders the real panel, so a fixed, small sample of the seeds is enough per run. */
const DOM_SEEDS = SEEDS.slice(0, 30);

import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {act, type JSX} from 'react';
import {createRoot} from 'react-dom/client';
import {useGatewayChatState} from './gateway-chat.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

type RefusalInit = MessageInitShape<typeof ConfigurationFailedSchema>;
type Call = FakeGatewayBridge['calls'][number];

/** The host: committed logs, and replies it releases only when the test says so. */
class Peer {
  readonly bridge = new FakeGatewayBridge();
  readonly sessions = new Map<string, StoreRecord[]>();
  readonly run: StoreRecord[] = [];
  readonly created: string[] = [];
  readonly controls = new Map<string, {text: string; mode: SteerMode}>();
  readonly withdrawals: Array<{control: string; withdraws: string}> = [];
  readonly answered = new Set<Call>();
  /** While set, the next host call of that kind is refused, as the host refuses a configuration. */
  refuse: {call: 'createChatSession' | 'chatMessage' | 'postIntent'; failure: RefusalInit} | null =
    null;
  /** The run's one reader: batches of the run log's records, as the page's fanout hands them out. */
  readonly runListeners = new Set<(records: readonly Uint8Array[]) => void>();
  readonly subscribeRun = (listener: (records: readonly Uint8Array[]) => void) => {
    this.runListeners.add(listener);
    return () => this.runListeners.delete(listener);
  };

  pending(): Call[] {
    return this.bridge.calls.filter(call => !this.answered.has(call) && !call.cancelled);
  }

  private record(
    log: StoreRecord[],
    body: Exclude<NonNullable<MessageInitShape<typeof RecordSchema>['body']>, {case: undefined}>,
  ): StoreRecord {
    const record = create(RecordSchema, {
      seq: BigInt(log.length + 1),
      epoch: 1,
      at: {seconds: 1n},
      body,
    });
    log.push(record);
    this.publish(log === this.run ? '' : this.idOf(log), record);
    return record;
  }

  private idOf(log: StoreRecord[]): string {
    return [...this.sessions].find(([, known]) => known === log)?.[0] ?? '';
  }

  /** Every live tail of that log gets the record, as the gateway's follow does. */
  private publish(sessionId: string, record: StoreRecord): void {
    if (sessionId === '') {
      for (const listener of [...this.runListeners]) listener([toBinary(RecordSchema, record)]);
      return;
    }
    for (const stream of this.bridge.streams) {
      if (stream.cancelled) continue;
      const request = fromBinary(TailRequestSchema, stream.bytes);
      if (request.sessionId === sessionId && record.seq >= request.fromSeq)
        stream.receive(this.tail([record]));
    }
  }

  private tail(records: readonly StoreRecord[]): Uint8Array {
    return toBinary(
      TailResponseSchema,
      create(TailResponseSchema, {
        records: records.map(row => toBinary(RecordSchema, row)),
        throughSeq: records.at(-1)?.seq ?? 0n,
      }),
    );
  }

  /** A tail opened at `fromSeq` replays what the log already holds. */
  replay(): void {
    for (const stream of this.bridge.streams) {
      if (stream.cancelled || (stream as {replayed?: boolean}).replayed) continue;
      (stream as {replayed?: boolean}).replayed = true;
      const request = fromBinary(TailRequestSchema, stream.bytes);
      if (request.sessionId === '') continue;
      const log = this.sessions.get(request.sessionId) ?? [];
      const rows = log.filter(row => row.seq >= request.fromSeq);
      if (rows.length > 0) stream.receive(this.tail(rows));
    }
  }

  /** Apply the call's effect on the host and send its reply. */
  answer(call: Call): void {
    this.answered.add(call);
    if (call.method === CHAT_SESSIONS_METHOD) {
      const sessions = this.created.map(id => ({id, provider: 'claude', model: 'opus', title: ''}));
      call.resolve(
        toBinary(
          ListChatSessionsResponseSchema,
          create(ListChatSessionsResponseSchema, {sessions}),
        ),
      );
    } else if (call.method === CHAT_CATALOG_METHOD)
      this.answerCatalog(call, [{provider: 'claude', model: 'opus', default: true}]);
    else if (call.method === HOST_CALL_METHOD) this.hostCall(call);
    else call.resolve(toBinary(GetBlobsResponseSchema, create(GetBlobsResponseSchema, {})));
  }

  /** The host's harness list, as the catalogue read returns it. */
  answerCatalog(
    call: Call,
    harnesses: readonly {provider: string; model: string; default: boolean}[],
  ): void {
    const request = fromBinary(ReadRequestSchema, call.bytes).method;
    if (request.case !== 'listHarnesses') throw new Error('Unexpected read');
    call.resolve(
      toBinary(
        ReadResponseSchema,
        create(ReadResponseSchema, {
          result: {
            case: 'listHarnesses',
            value: {requestId: request.value.requestId, harnesses: [...harnesses]},
          },
        }),
      ),
    );
  }

  /** Refuse the call if the test asked for that kind to be refused, once. */
  private refused(kind: string | undefined, reply: (result: object) => void): boolean {
    if (this.refuse === null || kind !== this.refuse.call) return false;
    const failure = this.refuse.failure;
    this.refuse = null;
    reply({result: {case: 'configurationFailed', value: failure}});
    return true;
  }

  private hostCall(call: Call): void {
    const method = fromBinary(HostCallRequestSchema, call.bytes).method;
    const reply = (result: Parameters<typeof create<typeof HostCallResponseSchema>>[1]) =>
      call.resolve(toBinary(HostCallResponseSchema, create(HostCallResponseSchema, result)));
    if (this.refused(method.case, reply)) return;
    if (method.case === 'createChatSession') {
      const id = method.value.sessionId;
      if (!this.sessions.has(id)) {
        const log: StoreRecord[] = [];
        this.sessions.set(id, log);
        this.created.push(id);
        this.record(log, {
          case: 'chat',
          value: {event: {case: 'created', value: {id, provider: 'claude', model: 'opus'}}},
        });
      }
      reply({result: {case: 'createChatSession', value: {sessionId: id}}});
    } else if (method.case === 'chatMessage') {
      const request = method.value;
      const log = this.sessions.get(request.sessionId);
      if (!log) throw new Error(`message for an unknown thread ${request.sessionId}`);
      if (!log.some(row => hasRequest(row, request.requestId)))
        this.record(log, {
          case: 'chat',
          value: {
            event: {
              case: 'message',
              value: {
                requestId: request.requestId,
                role: ChatRole.USER,
                content: {case: 'text', value: request.text},
              },
            },
          },
        });
      reply({
        result: {
          case: 'chatMessage',
          value: {sessionId: request.sessionId, requestId: request.requestId},
        },
      });
    } else if (method.case === 'postIntent') {
      const intent = method.value.intent;
      if (intent.case === 'withdraw')
        this.withdrawals.push({control: method.value.requestId, withdraws: intent.value.withdraws});
      if (intent.case === 'steer')
        this.controls.set(method.value.requestId, {
          text: intent.value.text,
          mode: intent.value.mode,
        });
      reply({
        result: {
          case: 'postIntent',
          value: {requestId: method.value.requestId, orderedReader: true},
        },
      });
    } else throw new Error('Unexpected host call');
  }

  /** The agent's answer to the thread's latest question, failed or not, as the session log records it. */
  writeAnswer(sessionId: string, text: string, failed: boolean): void {
    const log = this.sessions.get(sessionId);
    const asked = log
      ?.filter(
        row =>
          row.body.case === 'chat' &&
          row.body.value.event.case === 'message' &&
          row.body.value.event.value.role === ChatRole.USER,
      )
      .at(-1);
    if (
      log === undefined ||
      asked?.body.case !== 'chat' ||
      asked.body.value.event.case !== 'message'
    )
      throw new Error(`no question in ${sessionId}`);
    const requestId = asked.body.value.event.value.requestId;
    this.record(log, {
      case: 'chat',
      value: {
        event: {
          case: 'message',
          value: {
            requestId,
            role: ChatRole.ASSISTANT,
            content: {case: 'text', value: text},
            failed,
          },
        },
      },
    });
  }

  /** What the run writes about a control, as the run log records it. */
  writeIntent(
    controlId: string,
    step: IntentStep,
    options: {action?: IntentAction; reason?: string; withdraws?: string} = {},
  ): void {
    const known = this.controls.get(controlId);
    this.record(this.run, {
      case: 'intent',
      value: {
        controlId,
        action: options.action ?? IntentAction.STEER,
        step,
        ...(options.reason ? {reason: options.reason} : {}),
        ...(options.withdraws ? {withdraws: options.withdraws} : {}),
        ...(known ? {mode: known.mode} : {}),
      },
    });
  }

  /** The turn that received the control: a span carrying a steer link to it. */
  writeDelivery(controlId: string): void {
    this.record(this.run, {
      case: 'span',
      value: {
        change: 1,
        span: {
          id: `turn-${this.run.length}`,
          name: 'turn',
          label: {title: 'turn', links: [{rel: 'steer', to: controlId}]},
        },
      },
    });
  }
}

function hasRequest(row: StoreRecord, requestId: string): boolean {
  return (
    row.body.case === 'chat' &&
    row.body.value.event.case === 'message' &&
    row.body.value.event.value.requestId === requestId
  );
}

function press(container: Element, text: string): void {
  const found = [...container.querySelectorAll('button')].find(
    button => button.textContent?.trim() === text || button.getAttribute('aria-label') === text,
  );
  if (found === undefined) throw new Error(`no button ${text}`);
  found.click();
}

function type(element: Element, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(
    Object.getPrototypeOf(element) as object,
    'value',
  )?.set;
  (element as HTMLElement).focus();
  setter?.call(element, value);
  for (const kind of ['input', 'change', 'keyup'])
    element.dispatchEvent(new window.Event(kind, {bubbles: true}) as unknown as Event);
}

type State = ReturnType<typeof useGatewayChatState>;

class Screen {
  readonly peer = new Peer();
  readonly clock = new FakeClock();
  readonly container = document.createElement('div');
  readonly root = createRoot(this.container);
  state: State | null = null;
  constructor() {
    document.body.append(this.container);
  }

  async mount(): Promise<void> {
    const screen = this;
    function Harness(): JSX.Element {
      const state = useGatewayChatState({
        bridge: screen.peer.bridge,
        host: 'local' as HostKey,
        runId: 'run-a',
        projectRoot: '/project',
        schedule: screen.clock.schedule,
        records: screen.peer.subscribeRun,
        core: initialCoreState(),
        epoch: 1n,
        readOnly: false,
      });
      screen.state = state;
      return (
        <div>
          <button type="button" onClick={state.chat.toggle}>
            Chat
          </button>
          {state.chat.panel === null ? null : <ChatPanel {...state.chat.panel} />}
        </div>
      );
    }
    await act(async () => this.root.render(<Harness />));
  }

  async settle(): Promise<void> {
    for (let turn = 0; turn < 4; turn++) await act(async () => undefined);
  }

  /** Release the host's replies in a random order, and let the screen react to each. */
  async release(random: Seeded, all = false): Promise<void> {
    const calls = this.peer.pending();
    const count = all ? calls.length : random.int(calls.length + 1);
    for (let index = 0; index < count; index++) {
      const open = this.peer.pending();
      if (open.length === 0) break;
      const call = random.pick(open);
      await act(async () => this.peer.answer(call));
      await this.settle();
    }
    await act(async () => this.peer.replay());
    await this.settle();
  }

  async drain(random: Seeded): Promise<void> {
    for (let round = 0; round < 8; round++) await this.release(random, true);
  }

  /** The newest live tail drops and the screen reconnects it after its delay. */
  async reconnect(): Promise<void> {
    await act(async () => {
      this.peer.bridge.streams
        .filter(stream => !stream.cancelled)
        .at(-1)
        ?.ended('lost');
      this.clock.advanceBy(1000);
    });
    await this.settle();
  }

  creations(): number {
    return this.peer.bridge.calls.filter(
      call =>
        call.method === HOST_CALL_METHOD &&
        fromBinary(HostCallRequestSchema, call.bytes).method.case === 'createChatSession',
    ).length;
  }

  shown(): string | null {
    return this.state?.chat.panel?.shown ?? null;
  }

  async ask(text: string): Promise<void> {
    const box = this.container.querySelector('textarea');
    if (box === null) throw new Error(`no question box: ${this.container.textContent}`);
    await act(async () => type(box, text));
    await act(async () => press(this.container, 'Ask'));
    await this.settle();
  }
}

/** One generated user or network action on the chat; true when the panel is left blank for a new chat. */
async function chatStep(screen: Screen, random: Seeded, label: string, blank: boolean) {
  const action = random.int(5);
  if (action === 0 && screen.shown() !== null) {
    await act(async () => press(screen.container, 'New chat'));
    return true;
  }
  if (action <= 2) {
    const before = screen.creations();
    const onThread = screen.shown() !== null;
    await screen.ask(`question ${label}`);
    // A follow-up goes to the thread on screen, however far discovery has got.
    if (onThread) expect(screen.creations()).toBe(before);
    return false;
  }
  if (action === 3) await screen.release(random);
  else await screen.reconnect();
  return blank;
}

test('generated new chats, follow-ups and slow discovery create a thread only for a new chat and show it at once', async () => {
  for (const seed of DOM_SEEDS) {
    const random = new Seeded(seed);
    const screen = new Screen();
    await screen.mount();
    await act(async () => press(screen.container, 'Chat'));
    await screen.settle();
    await screen.release(random, true);
    let blank = true;
    for (let step = 0; step < 14; step++) {
      blank = await chatStep(screen, random, `${seed}-${step}`, blank);
      if (screen.container.querySelector('textarea') === null)
        throw new Error(`the panel closed at step ${step} (seed ${seed})`);
      const creating = screen.peer.pending().some(call => call.method === HOST_CALL_METHOD);
      if (!blank && !creating && screen.peer.created.length > 0) {
        await screen.settle();
        expect(screen.shown()).not.toBeNull();
      }
    }
    await screen.drain(random);
    await act(async () => screen.root.unmount());
  }
});

/** What the run does with one steer, in the order it can write it. */
type Fate = 'refused' | 'applied' | 'reached' | 'withdrawn';
const FATES: readonly Fate[] = ['refused', 'applied', 'reached', 'withdrawn'];

function withdrawButton(container: Element, text: string): HTMLElement | null {
  let node: Element | null =
    [...container.querySelectorAll('*')].find(
      element => element.children.length === 0 && element.textContent === text,
    ) ?? null;
  while (node !== null && node !== container) {
    const button = [...node.querySelectorAll('button')].find(
      item => item.textContent === 'Withdraw',
    );
    if (button !== undefined) return button as HTMLElement;
    node = node.parentElement;
  }
  return null;
}

function cardOf(screen: Screen, text: string): {status: string; withdrawable: boolean} | null {
  for (const item of screen.state?.chat.panel?.items ?? [])
    if (item.kind === 'card' && item.card.draft.text === text)
      return {
        status: item.card.view.status?.label ?? '',
        withdrawable: item.card.view.withdrawable,
      };
  return null;
}

interface Sent {
  readonly fate: Fate;
  readonly standing: boolean;
  readonly control: string;
}

/** A steer card typed, set to apply every turn or not, and sent; the control id the host saw. */
async function sendSteer(screen: Screen, random: Seeded, text: string): Promise<Sent> {
  const standing = random.bool();
  await act(async () => type(screen.container.querySelector('textarea') as Element, text));
  await act(async () => press(screen.container, 'New steer'));
  if (standing) {
    const every = screen.container.querySelector('input[value="standing"]');
    if (every === null) throw new Error('no standing mode');
    await act(async () => (every as HTMLElement).click());
  }
  await act(async () => press(screen.container, 'Send'));
  await screen.release(random, true);
  const control = [...screen.peer.controls].find(([, known]) => known.text === text)?.[0];
  if (control === undefined) throw new Error(`the steer ${text} never reached the host`);
  return {fate: random.pick(FATES), standing, control};
}

/** The records the run writes for a steer of that fate. */
function writersFor(screen: Screen, {fate, control}: Sent): Array<() => void> {
  if (fate === 'refused')
    return [() => screen.peer.writeIntent(control, IntentStep.REFUSED, {reason: 'no such role'})];
  const applied = () => screen.peer.writeIntent(control, IntentStep.APPLIED);
  return fate === 'applied' ? [applied] : [applied, () => screen.peer.writeDelivery(control)];
}

/** Withdraw a standing steer from its card, and the run records that it applied the withdrawal. */
async function withdraw(screen: Screen, random: Seeded, text: string, control: string) {
  const button = withdrawButton(screen.container, text);
  if (button === null) throw new Error(`no Withdraw on the standing steer ${text}`);
  await act(async () => button.click());
  await screen.release(random, true);
  const request = screen.peer.withdrawals.find(known => known.withdraws === control);
  if (request === undefined) throw new Error(`Withdraw of ${text} never reached the host`);
  await act(async () =>
    screen.peer.writeIntent(request.control, IntentStep.APPLIED, {
      action: IntentAction.WITHDRAW,
      withdraws: control,
    }),
  );
  await screen.settle();
}

function expectCard(screen: Screen, text: string, {fate, standing}: Sent) {
  const card = cardOf(screen, text);
  if (card === null) throw new Error(`no card for ${text}`);
  if (fate === 'refused') expect(card.status).toBe('Refused: no such role');
  if (fate === 'applied') expect(card.status).toBe('Waiting');
  if (fate === 'reached') {
    expect(card.status).toContain(standing ? 'In force' : 'Reached');
    expect(card.withdrawable).toBe(standing);
  }
  if (fate === 'withdrawn') {
    expect(card.status).toContain(standing ? 'Withdrawn' : 'Reached');
    expect(card.withdrawable).toBe(false);
  }
}

async function steerRun(seed: number) {
  const random = new Seeded(seed);
  const screen = new Screen();
  await screen.mount();
  await act(async () => press(screen.container, 'Chat'));
  await screen.settle();
  await screen.release(random, true);
  await screen.ask(`hello ${seed}`);
  await screen.drain(random);
  const steers = new Map<string, Sent>();
  for (let index = 0; index < 2 + random.int(3); index++) {
    const text = `steer ${seed}-${index}`;
    steers.set(text, await sendSteer(screen, random, text));
  }
  // The run writes its records for every steer, interleaved; the screen may lose its tail between.
  for (const write of [...steers.values()].flatMap(sent => writersFor(screen, sent))) {
    await act(async () => write());
    await screen.settle();
    if (random.int(3) === 0) await screen.reconnect();
  }
  await screen.release(random, true);
  for (const [text, sent] of steers)
    if (sent.fate === 'withdrawn' && sent.standing)
      await withdraw(screen, random, text, sent.control);
  for (const [text, sent] of steers) expectCard(screen, text, sent);
  await act(async () => screen.root.unmount());
}

test('generated steers, refusals, deliveries, withdrawals and reconnects show on the cards what the run recorded', async () => {
  for (const seed of DOM_SEEDS) await steerRun(seed);
});

function leafTexts(container: Element): string[] {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const texts: string[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode())
    texts.push(node.textContent ?? '');
  return texts;
}

function buttonLabels(container: Element): string[] {
  return [...container.querySelectorAll('button')].map(button => button.textContent?.trim() ?? '');
}

test("generated refusals of thread creation, questions and steers show the host's message and its diagnostic", async () => {
  for (const seed of DOM_SEEDS) {
    const random = new Seeded(seed);
    const call = random.pick(['createChatSession', 'chatMessage', 'postIntent'] as const);
    const stderr = random.bool()
      ? ''
      : [1, 2, 3].map(line => `stderr ${seed} line ${line}`).join('\n');
    const failure = {
      code: random.pick(['invalid_method', 'host_unsupported', 'chat_unavailable']),
      stage: 'chat',
      message: `The host refused ${seed}`,
      stderr,
    };
    const screen = new Screen();
    await screen.mount();
    await act(async () => press(screen.container, 'Chat'));
    await screen.settle();
    await screen.release(random, true);
    if (call !== 'createChatSession') {
      await screen.ask(`hello ${seed}`);
      await screen.drain(random);
    }
    screen.peer.refuse = {call, failure};
    if (call === 'postIntent') {
      await act(async () =>
        type(screen.container.querySelector('textarea') as Element, `steer ${seed}`),
      );
      await act(async () => press(screen.container, 'New steer'));
      await act(async () => press(screen.container, 'Send'));
    } else await screen.ask(`question ${seed}`);
    await screen.release(random, true);
    const message = new IntentRefused(create(ConfigurationFailedSchema, failure)).message;
    const texts = leafTexts(screen.container);
    expect(texts).toContain(message);
    expect(texts.filter(text => /^(IntentRefused|Error):/.test(text))).toEqual([]);
    // A refused steer can be asked about; a question about the chat's own refused request would
    // be refused the same way, so its details are copied instead.
    const labels = buttonLabels(screen.container);
    expect(labels.includes('Ask about this error')).toBe(call === 'postIntent');
    expect(labels.includes('Copy details')).toBe(call !== 'postIntent');
    const details = [...screen.container.querySelectorAll('button')].filter(
      button => button.textContent?.trim() === 'Details',
    );
    if (stderr === '') expect(details).toHaveLength(0);
    else {
      expect(details).toHaveLength(1);
      await act(async () => press(screen.container, 'Details'));
      const shown = screen.container.textContent ?? '';
      for (const line of stderr.split('\n')) expect(shown).toContain(line);
    }
    await act(async () => screen.root.unmount());
  }
});

test('a new chat with no harness to pick, and none known for the run, says why', async () => {
  for (const unread of [true, false]) {
    const screen = new Screen();
    await screen.mount();
    await act(async () => press(screen.container, 'Chat'));
    await screen.settle();
    for (const call of screen.peer.pending().filter(each => each.method === CHAT_CATALOG_METHOD)) {
      screen.peer.answered.add(call);
      if (unread) await act(async () => call.reject(new Error('host unreachable')));
      else await act(async () => screen.peer.answerCatalog(call, []));
    }
    await screen.settle();
    await screen.ask('why?');
    expect(leafTexts(screen.container)).toContain(
      unread ? HARNESS_LIST_UNREAD : NO_HARNESS_OFFERED,
    );
    expect(buttonLabels(screen.container)).not.toContain('Ask about this error');
    expect(screen.creations()).toBe(0);
    await act(async () => screen.root.unmount());
  }
});

test('generated threads show each failed answer as failed and the rest as answers', async () => {
  for (const seed of DOM_SEEDS) {
    const random = new Seeded(seed);
    const screen = new Screen();
    await screen.mount();
    await act(async () => press(screen.container, 'Chat'));
    await screen.settle();
    await screen.release(random, true);
    const failed: boolean[] = [];
    for (let turn = 0; turn < 1 + random.int(4); turn++) {
      await screen.ask(`question ${seed}-${turn}`);
      await screen.drain(random);
      const thread = screen.peer.created.at(-1);
      if (thread === undefined) throw new Error('no thread was created');
      failed.push(random.bool());
      await act(async () =>
        screen.peer.writeAnswer(thread, `answer ${seed}-${turn}`, failed.at(-1) === true),
      );
      await screen.settle();
    }
    const answers = [
      ...screen.container.querySelectorAll<HTMLElement>('.vs-chat-message[data-side="start"]'),
    ];
    expect(answers.map(answer => answer.getAttribute('data-tone') === 'err')).toEqual(failed);
    expect(answers.map(answer => leafTexts(answer).includes('Failed'))).toEqual(failed);
    await act(async () => screen.root.unmount());
  }
});
