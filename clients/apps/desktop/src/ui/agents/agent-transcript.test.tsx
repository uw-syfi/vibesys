import {describe, expect, it} from '@effect/vitest';
import {AgentStatus} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {
  agentNames,
  applyChange,
  type Entry,
  emptyState,
  itemIs,
  type OrchestratorState,
  type Transcript,
  transcript,
} from '../../state/index.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {TEST_CLOCK} from '../test-display-clock.js';
import {AgentTranscript, type HistoryAccess, itemsOf} from './agent-transcript.js';
import {agentLabel} from './labels.js';

const PROTOCOL = /UNSPECIFIED|AGENT_ROLE|TURN_|ITEM_STATUS|undefined|\[object|NaN|\bnull\b/;

const idle: HistoryAccess = {
  earlier: {_tag: 'Idle'},
  wholes: {},
  loadEarlier: () => undefined,
  expand: () => undefined,
};

function worldOf(seed: number, subagents: number, live: boolean) {
  const scenario = generateScenario({seed, subagents, live, tree: true, depth: 3});
  const state = scenario.changes.reduce(applyChange, emptyState);
  return {scenario, state};
}

function contextOf(state: OrchestratorState, agentId: string, history: HistoryAccess = idle) {
  const run = state.agents[agentId]?.runId ?? '';
  const names = agentNames(state, run);
  return {
    agentId,
    host: 'host-a',
    clock: TEST_CLOCK,
    labelOf: (id: string) => {
      const name = names.get(id);
      return name === undefined ? 'An agent' : agentLabel(name);
    },
    open: () => undefined,
    history,
  };
}

describe('the transcript screen', () => {
  it('draws any agent of any run in words, and a row per entry with its own id', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 40}),
        fc.integer({min: 8, max: 16}),
        fc.boolean(),
        (seed, subagents, live) => {
          const {scenario, state} = worldOf(seed, subagents, live);
          for (const agent of Object.values(state.agents)) {
            const html = renderToStaticMarkup(
              <AgentTranscript
                state={state}
                agentId={agent.id}
                host="host-a"
                nowMs={scenario.endMs + 60_000}
                history={idle}
                open={() => undefined}
              />,
            );
            expect(html).not.toMatch(PROTOCOL);
            const data = transcript(state, agent.id, scenario.endMs);
            if (data === null) continue;
            const items = itemsOf(data, contextOf(state, agent.id));
            expect(new Set(items.map(item => item.id)).size).toBe(items.length);
          }
        },
      ),
      {numRuns: 8},
    );
  });

  it('writes a turn rule with Seattle time and the cost the host reported', () => {
    const {scenario, state} = worldOf(5, 10, false);
    const lead = Object.values(state.agents).find(agent => agent.parentAgentId === '');
    const data = transcript(state, lead?.id ?? '', scenario.endMs);
    const rules = itemsOf(must(data), contextOf(state, lead?.id ?? '')).flatMap(item =>
      item.kind === 'rule' ? [item.label] : [],
    );
    expect(rules.length).toBeGreaterThan(1);
    for (const label of rules)
      expect(label).toMatch(/^Turn \d+ · \w{3} \d{1,2}:\d{2} (AM|PM)( · \$\d+\.\d{2})?$/);
  });

  it('shows a call as one line with its name and first argument line, however long or empty', () => {
    const {scenario, state} = worldOf(5, 12, false);
    const worker = Object.values(state.agents).find(agent => agent.id.includes('worker'));
    const data = transcript(state, worker?.id ?? '', scenario.endMs);
    const tools = itemsOf(must(data), contextOf(state, worker?.id ?? '')).flatMap(item =>
      item.kind === 'tool' ? [item] : [],
    );
    expect(tools.length).toBeGreaterThan(5);
    for (const tool of tools) {
      expect(tool.summary).not.toContain('\n');
      expect(tool.name).not.toBe('');
    }
    expect(tools.some(tool => tool.summary.length > 200)).toBe(true);
    expect(tools.some(tool => tool.summary === '')).toBe(true);
    expect(tools.some(tool => tool.state === 'failed')).toBe(true);
  });

  it('says a body that was left out is not loaded, then that it is loading, then nothing once read', () => {
    const {scenario, state} = worldOf(5, 12, false);
    const worker = Object.values(state.agents).find(agent => agent.id.includes('worker')) ?? null;
    const id = worker?.id ?? '';
    // The run stream leaves nothing out; a page does, so mark one item as a page would.
    const target = Object.values(state.items).find(
      one => one.agentId === id && itemIs(one, 'tool'),
    );
    const elided = {...state, elided: {[target?.id ?? '']: 5_000}};
    const data = transcript(elided, id, scenario.endMs);
    const wordsOf = (history: HistoryAccess) =>
      itemsOf(must(data), contextOf(elided, id, history)).flatMap(row =>
        row.kind === 'tool' && row.notice !== undefined ? [row.notice] : [],
      );
    expect(wordsOf(idle)).toEqual(['5 KB not loaded']);
    const ordinal = target?.ordinal ?? 0;
    expect(wordsOf({...idle, wholes: {[ordinal]: {_tag: 'Loading'}}})).toEqual(['Loading 5 KB']);
    expect(
      wordsOf({
        ...idle,
        wholes: {
          [ordinal]: {_tag: 'Failed', fault: {kind: 'Unavailable', detail: '', retryable: true}},
        },
      })[0],
    ).toMatch(/^Could not load 5 KB/);
  });

  it('marks content whose turn details were not loaded without inventing a turn number', () => {
    const data: Transcript = {
      agentId: 'lead',
      crumbs: [],
      state: 'running',
      costUsd: null,
      turns: 0,
      entries: [
        {
          kind: 'turn-unavailable',
          key: 'turn-unavailable:missing',
          turnId: 'missing',
          items: 3,
        },
      ],
      earlier: null,
    };
    const [rule] = itemsOf(data, contextOf(emptyState, 'lead'));
    expect(rule).toEqual({
      kind: 'rule',
      id: 'turn-unavailable:missing',
      label: 'Turn details not loaded · 3 items',
    });
  });
});

/** One turn of a lead, whose entries say what came of a child named Worker 2. */
function childWorld(entries: readonly Entry[]) {
  const data: Transcript = {
    agentId: 'lead',
    crumbs: [],
    state: 'running',
    costUsd: null,
    turns: 1,
    entries: [
      {kind: 'turn', key: 't', turnId: 't', ordinal: 1, atMs: 0, costUsd: null, outcome: 'running'},
      ...entries,
    ],
    earlier: null,
  };
  const context = {...contextOf(emptyState, 'lead'), labelOf: () => 'Worker 2'};
  return itemsOf(data, context);
}

/** A reason as the host may give it: none, blank, one line, or long and over several lines. */
const reason = fc.oneof(
  fc.constant(''),
  fc.constant('  \n'),
  fc.string({minLength: 1, maxLength: 80}),
  fc
    .array(fc.string({minLength: 1, maxLength: 120}), {minLength: 2, maxLength: 8})
    .map(lines => lines.join('\n')),
);

describe('a child that stopped working without a handoff', () => {
  const HOW = [
    [AgentStatus.IDLE, 'went idle'],
    [AgentStatus.ENDED, 'ended'],
    [AgentStatus.FAILED, 'failed'],
    [AgentStatus.LOST, 'was lost'],
  ] as const;

  it('is told to the lead by name, with how it settled and the reason the host gave', () => {
    fc.assert(
      fc.property(fc.constantFrom(...HOW), reason, ([status, how], detail) => {
        const [, bubble] = childWorld([
          {
            kind: 'incoming',
            key: 'in',
            atMs: 0,
            from: {kind: 'host'},
            note: {kind: 'settled', agentId: 'w2', status, detail},
          },
        ]);
        if (bubble?.kind !== 'bubble') throw new Error('the message is drawn as a bubble');
        const said = detail.trim() === '' ? '.' : `: ${detail.trim()}`;
        expect(bubble.text).toBe(`Worker 2 ${how} without a handoff${said}`);
      }),
    );
  });

  it('comes back on a row that says how, with the reason under it when the host gave one', () => {
    const CHIP = {
      'handed-back': 'Done',
      idle: 'Idle',
      ended: 'Ended',
      failed: 'Failed',
      lost: 'Lost',
    } as const;
    fc.assert(
      fc.property(
        fc.constantFrom('handed-back', 'idle', 'ended', 'failed', 'lost'),
        reason,
        (result, detail) => {
          const [, row] = childWorld([
            {kind: 'join', key: 'j', atMs: 0, childId: 'w2', result, detail},
          ]);
          if (row?.kind !== 'link') throw new Error('a join is drawn as a link');
          expect(row.title).toBe('Worker 2 joined');
          expect(row.chip?.label).toBe(CHIP[result]);
          expect(row.note).toBe(detail.trim() === '' ? undefined : detail.trim());
        },
      ),
    );
  });
});

describe('the turns of a transcript', () => {
  it('each has a divider before it, the first included, and the conversation opens on one', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 40}),
        fc.integer({min: 1, max: 12}),
        fc.boolean(),
        (seed, subagents, live) => {
          const {scenario, state} = worldOf(seed, subagents, live);
          for (const agent of Object.values(state.agents)) {
            const data = must(transcript(state, agent.id, scenario.endMs));
            const kinds = itemsOf(data, contextOf(state, agent.id)).map(row => row.kind);
            const dividers = kinds.filter(kind => kind === 'rule').length;
            expect(dividers).toBe(data.entries.filter(entry => entry.kind === 'turn').length);
            // No row of a turn precedes its divider: the conversation opens on the first one.
            if (kinds.length > 0) expect(kinds[0]).toBe('rule');
          }
        },
      ),
      {numRuns: 15},
    );
  });
});

function must<T>(value: T | null): T {
  if (value === null) throw new Error('the agent is in the state');
  return value;
}
