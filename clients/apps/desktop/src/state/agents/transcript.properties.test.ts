import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  Delivery,
  HandoffSchema,
  Harness,
  type Item,
  ItemStatus,
  type Message,
  MessageSchema,
  SubagentSettledSchema,
  TurnSchema,
  TurnStatus,
  TurnTrigger,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {itemOf} from '../../testing/orchestrator/builders.js';
import {emptyState, type OrchestratorState} from '../orchestrator/state.js';
import {type Entry, type Transcript, transcript} from './transcript.js';

const RUN = 'run';
const ME = 'me';
const NOW = 5_000_000;
const KINDS = ['assistant', 'reasoning', 'prompt', 'tool'] as const;

interface ItemSpec {
  readonly kind: number;
  readonly text: string;
  readonly status: number;
  readonly length: number;
  readonly started: boolean;
  readonly elided: boolean;
  readonly bumpTurn: boolean;
}

interface ChildSpec {
  readonly start: number;
  readonly result: 'none' | 'handoff' | 'failed' | 'ended';
  readonly resultAt: number;
}

interface World {
  readonly turns: number;
  readonly items: readonly ItemSpec[];
  /** How many of the newest items are loaded. */
  readonly loaded: number;
  readonly children: readonly ChildSpec[];
  readonly triggers: readonly boolean[];
  readonly parentChain: number;
}

const itemSpec: fc.Arbitrary<ItemSpec> = fc.record({
  kind: fc.integer({min: 0, max: 3}),
  text: fc.constantFrom('', 'words', 'a longer line\nwith two lines'),
  status: fc.integer({min: 0, max: 2}),
  length: fc.integer({min: 0, max: 5_000}),
  started: fc.boolean(),
  elided: fc.boolean(),
  bumpTurn: fc.boolean(),
});

const childSpec: fc.Arbitrary<ChildSpec> = fc.record({
  start: fc.integer({min: 0, max: 400_000}),
  result: fc.constantFrom('none', 'handoff', 'failed', 'ended'),
  resultAt: fc.integer({min: 0, max: 500_000}),
});

const world: fc.Arbitrary<World> = fc.record({
  turns: fc.integer({min: 1, max: 6}),
  items: fc.array(itemSpec, {maxLength: 30}),
  loaded: fc.integer({min: 0, max: 30}),
  children: fc.array(childSpec, {maxLength: 6}),
  triggers: fc.array(fc.boolean(), {minLength: 6, maxLength: 6}),
  parentChain: fc.integer({min: 0, max: 4}),
});

const agent = (id: string, parent: string, start: number) =>
  create(AgentSchema, {
    id,
    runId: RUN,
    parentAgentId: parent,
    role: parent === '' ? AgentRole.LEAD : AgentRole.WORKER,
    harness: Harness.CODEX,
    status: AgentStatus.IDLE,
    stopStage: AgentStopStage.NONE,
    session: 1,
    startedAt: timestampFromMs(start),
  });

const STATUS = [ItemStatus.FINISHED, ItemStatus.RUNNING, ItemStatus.FAILED] as const;

/** Items 1..n, split over the turns in order; only the newest `loaded` are in the state. */
function build(spec: World): {state: OrchestratorState; items: Item[]} {
  const turnLength = Math.max(1, Math.ceil(spec.items.length / spec.turns));
  const all = spec.items.map((one, index) =>
    itemOf({
      id: `i${index + 1}`,
      turnId: `t${Math.min(spec.turns, Math.floor(index / turnLength) + 1)}`,
      agentId: ME,
      ordinal: index + 1,
      atMs: 1_000 * (index + 1) + one.length,
      kind: KINDS[one.kind] ?? 'tool',
      text: one.text,
      status: STATUS[one.status] ?? ItemStatus.FINISHED,
      output: one.text,
      ...(one.started ? {startedMs: 1_000 * (index + 1)} : {}),
    }),
  );
  const items = all.slice(Math.max(0, all.length - spec.loaded));
  const turns = Array.from({length: spec.turns}, (_, index) =>
    create(TurnSchema, {
      id: `t${index + 1}`,
      agentId: ME,
      ordinal: index + 1,
      trigger: TurnTrigger.MESSAGE,
      triggerMessageId: spec.triggers[index] === true ? `m${index + 1}` : 'missing',
      status: index === spec.turns - 1 ? TurnStatus.RUNNING : TurnStatus.COMPLETED,
      startedAt: timestampFromMs(1_000 * (index * turnLength + 1) - 500),
      usage: create(UsageSchema, {costUsd: (index + 1) / 100}),
    }),
  );
  const parents = Array.from({length: spec.parentChain}, (_, index) =>
    agent(`p${index}`, index === 0 ? '' : `p${index - 1}`, 0),
  );
  const children = spec.children.map((one, index) => agent(`c${index}`, ME, one.start));
  const me = agent(ME, parents.at(-1)?.id ?? '', 0);
  const messages = [
    ...turns.flatMap((_turn, index) =>
      spec.triggers[index] === true
        ? [
            create(MessageSchema, {
              id: `m${index + 1}`,
              runId: RUN,
              toAgentId: ME,
              fromAgentId: '',
              delivery: Delivery.CONSUMED,
              at: timestampFromMs(1_000 * index),
              body: {case: 'text', value: `to turn ${index + 1}`},
            }),
          ]
        : [],
    ),
    ...spec.children.flatMap((one, index) => resultOf(one, `c${index}`, index)),
  ];
  const state: OrchestratorState = {
    ...emptyState,
    runs: {},
    agents: Object.fromEntries([...parents, me, ...children].map(one => [one.id, one])),
    turns: Object.fromEntries(turns.map(turn => [turn.id, turn])),
    items: Object.fromEntries(items.map(item => [item.id, item])),
    elided: Object.fromEntries(
      items.flatMap((item, index) =>
        spec.items[all.indexOf(item)]?.elided === true ? [[item.id, 5_000 + index]] : [],
      ),
    ),
    messages: Object.fromEntries(messages.map(message => [message.id, message])),
  };
  return {state, items};
}

function resultOf(one: ChildSpec, childId: string, index: number): Message[] {
  const base = {runId: RUN, delivery: Delivery.CONSUMED, at: timestampFromMs(one.resultAt)};
  if (one.result === 'handoff') {
    return [
      create(MessageSchema, {
        ...base,
        id: `h${index}`,
        fromAgentId: childId,
        toAgentId: ME,
        body: {case: 'handoff', value: create(HandoffSchema, {claim: 'did it'})},
      }),
    ];
  }
  if (one.result === 'none') return [];
  return [
    create(MessageSchema, {
      ...base,
      id: `s${index}`,
      toAgentId: ME,
      body: {
        case: 'subagentSettled',
        value: create(SubagentSettledSchema, {
          agentId: childId,
          status: one.result === 'failed' ? AgentStatus.FAILED : AgentStatus.ENDED,
        }),
      },
    }),
  ];
}

const read = (spec: World): {result: Transcript; items: Item[]; state: OrchestratorState} => {
  const {state, items} = build(spec);
  const result = transcript(state, ME, NOW);
  if (result === null) throw new Error('the agent is in the state');
  return {result, items, state};
};

const itemEntries = (entries: readonly Entry[]): Entry[] =>
  entries.filter(
    entry => entry.kind === 'said' || entry.kind === 'thought' || entry.kind === 'tool',
  );

const hasWords = (item: Item, state: OrchestratorState): boolean => {
  const {body} = item;
  if (body.case === 'tool') return true;
  if (body.case === 'assistant') return body.value.text !== '';
  if (body.case === 'reasoning')
    return body.value.text !== '' || state.elided[item.id] !== undefined;
  return false;
};

/** An event comes after every item of its turn that began at or before it, and before the first that began after. */
function expectEventPlace(
  entries: readonly Entry[],
  started: ReadonlyMap<string, number>,
  entry: Entry,
  index: number,
): void {
  if (entry.kind !== 'spawn' && entry.kind !== 'join') return;
  const first = entries
    .slice(0, index)
    .reduce((found, other, at) => (other.kind === 'turn' ? at : found), 0);
  const turnEnd = entries.findIndex((other, at) => at > index && other.kind === 'turn');
  const items = (from: number, to: number | undefined) =>
    entries.slice(from, to).filter(other => started.has(other.key));
  for (const other of items(first, index)) {
    expect(started.get(other.key) ?? 0).toBeLessThanOrEqual(entry.atMs);
  }
  const after = items(index + 1, turnEnd === -1 ? undefined : turnEnd)[0];
  if (after !== undefined) expect(entry.atMs).toBeLessThan(started.get(after.key) ?? 0);
}

/** A span is there only when the host said when the item began. */
function expectSpan(entry: Entry, items: readonly Item[]): void {
  if (entry.kind !== 'thought' && entry.kind !== 'tool') return;
  const item = items.find(one => `item:${one.id}` === entry.key);
  if (item?.startedAt === undefined) expect(entry.spanMs).toBeNull();
  else if (entry.kind === 'tool' || !entry.live) expect(entry.spanMs).toBeGreaterThanOrEqual(0);
}

describe('a transcript', () => {
  it('shows each loaded item that has words exactly once and in order, and no prompt item', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, items, state} = read(spec);
        const expected = items.filter(item => hasWords(item, state)).map(item => `item:${item.id}`);
        expect(itemEntries(result.entries).map(entry => entry.key)).toEqual(expected);
      }),
    );
  });

  it('puts the turns in order and each item under the divider of its own turn', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, items} = read(spec);
        const turnOf = new Map(items.map(item => [`item:${item.id}`, item.turnId]));
        let current = '';
        let last = 0;
        for (const entry of result.entries) {
          if (entry.kind === 'turn') {
            expect(entry.ordinal).toBeGreaterThan(last);
            last = entry.ordinal;
            current = entry.turnId;
          } else {
            expect(current).not.toBe('');
            const turn = turnOf.get(entry.key);
            if (turn !== undefined) expect(turn).toBe(current);
          }
        }
      }),
    );
  });

  it('shows a turn with no loaded items only when it is newer than every loaded one', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, items} = read(spec);
        const withItems = new Set(items.map(item => item.turnId));
        const dividers = result.entries.flatMap(entry => (entry.kind === 'turn' ? [entry] : []));
        const newestLoaded = Math.max(
          0,
          ...dividers.filter(entry => withItems.has(entry.turnId)).map(entry => entry.ordinal),
        );
        for (const divider of dividers) {
          if (withItems.has(divider.turnId)) continue;
          if (withItems.size === 0) expect(divider.ordinal).toBe(spec.turns);
          else expect(divider.ordinal).toBeGreaterThan(newestLoaded);
        }
        for (const turn of withItems) {
          expect(dividers.some(entry => entry.turnId === turn)).toBe(true);
        }
      }),
    );
  });

  it('says what started a turn right after its divider, from the message when there is one', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, state} = read(spec);
        result.entries.forEach((entry, index) => {
          if (entry.kind !== 'turn') return;
          const next = result.entries[index + 1];
          const message = state.messages[state.turns[entry.turnId]?.triggerMessageId ?? ''];
          if (message !== undefined) {
            expect(next?.kind).toBe('incoming');
            expect(next?.kind === 'incoming' ? next.note : null).toEqual({
              kind: 'text',
              text: `to turn ${entry.ordinal}`,
            });
          }
        });
      }),
    );
  });

  it('shows one spawn row per child and one join row per result, in the turn that was running', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result} = read(spec);
        const spawns = result.entries.flatMap(entry => (entry.kind === 'spawn' ? [entry] : []));
        expect(spawns.map(entry => entry.childId).sort()).toEqual(
          spec.children.map((_, index) => `c${index}`).sort(),
        );
        const joins = result.entries.flatMap(entry => (entry.kind === 'join' ? [entry] : []));
        expect(joins).toHaveLength(spec.children.filter(one => one.result !== 'none').length);
        const dividers = result.entries.flatMap((entry, index) =>
          entry.kind === 'turn' ? [{atMs: entry.atMs, index}] : [],
        );
        result.entries.forEach((entry, index) => {
          if (entry.kind !== 'spawn' && entry.kind !== 'join') return;
          const own = dividers.filter(divider => divider.index < index).at(-1);
          const began =
            dividers.filter(divider => divider.atMs <= entry.atMs).at(-1) ?? dividers[0];
          expect(own?.index).toBe(began?.index);
        });
      }),
    );
  });

  it('shows a result of a child that failed or ended without handing back, and says which', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result} = read(spec);
        const joins = result.entries.flatMap(entry => (entry.kind === 'join' ? [entry] : []));
        spec.children.forEach((one, index) => {
          const join = joins.find(entry => entry.childId === `c${index}`);
          if (one.result === 'none') expect(join).toBeUndefined();
          else {
            expect(join?.result).toBe(one.result === 'handoff' ? 'handed-back' : one.result);
          }
        });
      }),
    );
  });

  it('puts an event before the first item that began after it, and after all the others', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, items} = read(spec);
        const started = new Map(
          items.map(item => [
            `item:${item.id}`,
            Number(item.startedAt?.seconds ?? item.at?.seconds ?? 0n) * 1000,
          ]),
        );
        result.entries.forEach((entry, index) => {
          expectEventPlace(result.entries, started, entry, index);
        });
      }),
    );
  });

  it('says how long a thought or a call took only when the host said when it began', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, items} = read(spec);
        for (const entry of result.entries) expectSpan(entry, items);
      }),
    );
  });

  it('names what a page left out of an item, and nothing for a whole one', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, state} = read(spec);
        for (const entry of result.entries) {
          if (entry.kind !== 'thought' && entry.kind !== 'tool') continue;
          expect(entry.elidedBytes).toBe(state.elided[entry.key.replace('item:', '')] ?? null);
        }
      }),
    );
  });

  it('says where earlier items can be read from, and nothing when all are loaded', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result, items} = read(spec);
        const lowest = items[0]?.ordinal;
        if (lowest === undefined || lowest === 1) expect(result.earlier).toBeNull();
        else expect(result.earlier).toBe(lowest);
      }),
    );
  });

  it('names the agent last in its place from the root, however the parents are linked', () => {
    fc.assert(
      fc.property(world, spec => {
        const {result} = read(spec);
        expect(result.crumbs).toHaveLength(spec.parentChain + 1);
        expect(result.crumbs.at(-1)?.agentId).toBe(ME);
        expect(result.crumbs[0]?.agentId).toBe(spec.parentChain === 0 ? ME : 'p0');
        expect(result.turns).toBe(spec.turns);
      }),
    );
  });

  it('is absent for an agent that is not there', () => {
    expect(transcript(emptyState, 'nobody', NOW)).toBeNull();
  });
});
