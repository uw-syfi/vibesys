import {describe, expect, test} from 'bun:test';
import type {Span} from '@vibesys/contracts';
import {initialCoreState} from '@vibesys/run-state';
import {agentGraph} from '@vibesys/run-state/agents';
import {agentTree, FOLD_FROM, ROOT_FOLD, type TreeRow} from './agent-tree.js';
import {agentModel, agentTurns} from './agent-turns.js';

/** One agent's turn span, as the search labels it. */
function turn(
  agent: string,
  at: number,
  extra: {startedBy?: string[]; status?: Span['status']; title?: string; role?: string} = {},
): Span {
  return {
    id: `turn:${agent}:${at}`,
    parent: null,
    type: 'operation',
    name: 'agent.turn',
    status: extra.status ?? 'done',
    started_at: at,
    ended_at: extra.status === 'running' ? null : at + 1,
    label: {
      kind: 'agent_turn',
      title: extra.title ?? 'a headline',
      attrs_json: JSON.stringify({
        agent,
        role: extra.role ?? agent,
        started_by: extra.startedBy ?? [],
      }),
    },
  };
}

function rowsOf(
  spans: readonly Span[],
  expanded: string[] = [],
  selected: string | null = null,
): readonly TreeRow[] {
  const byId = new Map(spans.map(span => [span.id, span]));
  return agentTree(agentGraph(spans), byId, new Set(expanded), selected);
}

const names = (rows: readonly TreeRow[]): string[] =>
  rows.map(row => (row.kind === 'agent' ? row.agent.id : `+${row.count}`));

describe('agentTree', () => {
  test('children sit under the agent that started them, in start order, with guides', () => {
    const rows = rowsOf([
      turn('boss', 1, {status: 'running'}),
      turn('a', 2, {startedBy: ['boss'], status: 'running'}),
      turn('a-kid', 3, {startedBy: ['a'], status: 'running'}),
      turn('b', 4, {startedBy: ['boss'], status: 'running'}),
    ]);
    expect(names(rows)).toEqual(['boss', 'a', 'a-kid', 'b']);
    expect(rows.map(row => row.guides)).toEqual([[], ['tee'], ['pipe', 'elbow'], ['elbow']]);
  });

  test('an agent with several parents sits under the first and names the others', () => {
    const rows = rowsOf([
      turn('boss', 1, {status: 'running', role: 'orchestrator'}),
      turn('impl', 2, {startedBy: ['boss'], status: 'running', role: 'implementer'}),
      turn('judge', 3, {startedBy: ['boss', 'impl'], status: 'running', role: 'judge'}),
    ]);
    const judge = rows.find(row => row.kind === 'agent' && row.agent.id === 'judge');
    expect(judge?.kind === 'agent' && judge.alsoUnder).toEqual(['Builder']);
    expect(rows.filter(row => row.kind === 'agent' && row.agent.id === 'judge')).toHaveLength(1);
    expect(names(rows)).toEqual(['boss', 'impl', 'judge']);
  });

  test('finished siblings fold from FOLD_FROM, active ones stay, and expanding shows them', () => {
    const kids = Array.from({length: FOLD_FROM}, (_, n) =>
      turn(`done-${n}`, 2 + n, {startedBy: ['boss']}),
    );
    const spans = [
      turn('boss', 1, {status: 'running'}),
      ...kids,
      turn('live', 20, {startedBy: ['boss'], status: 'running'}),
    ];
    expect(names(rowsOf(spans))).toEqual(['boss', 'live', `+${FOLD_FROM}`]);
    expect(names(rowsOf(spans, ['boss']))).toEqual([
      'boss',
      ...kids.map((_, n) => `done-${n}`),
      'live',
    ]);
  });

  test('a finished subtree with a running descendant is not folded', () => {
    const spans = [
      turn('boss', 1, {status: 'running'}),
      ...Array.from({length: FOLD_FROM}, (_, n) => turn(`d${n}`, 2 + n, {startedBy: ['boss']})),
      turn('deep', 30, {startedBy: ['d0'], status: 'running'}),
    ];
    expect(names(rowsOf(spans))).toContain('deep');
    expect(names(rowsOf(spans))).toContain('d0');
  });

  test('a failed agent is not folded away', () => {
    const spans = [
      turn('boss', 1, {status: 'running'}),
      ...Array.from({length: FOLD_FROM}, (_, n) => turn(`d${n}`, 2 + n, {startedBy: ['boss']})),
      turn('bad', 9, {startedBy: ['boss'], status: 'failed'}),
    ];
    expect(names(rowsOf(spans))).toEqual(['boss', 'bad', `+${FOLD_FROM}`]);
  });

  test('roots fold under the root key and count whole subtrees', () => {
    const spans = [
      ...Array.from({length: FOLD_FROM}, (_, n) => turn(`r${n}`, n * 2)),
      ...Array.from({length: FOLD_FROM}, (_, n) =>
        turn(`c${n}`, n * 2 + 1, {startedBy: [`r${n}`]}),
      ),
      turn('live', 50, {status: 'running'}),
    ];
    expect(names(rowsOf(spans))).toEqual(['live', `+${FOLD_FROM * 2}`]);
    expect(rowsOf(spans).at(-1)).toMatchObject({kind: 'fold', parent: ROOT_FOLD});
  });

  test('every agent is drawn exactly once, whatever starts what, even in a ring or from unknowns', () => {
    let seed = 7;
    const next = (bound: number): number => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % bound;
    };
    for (let run = 0; run < 50; run++) {
      const size = 1 + next(30);
      const spans = Array.from({length: size}, (_, n) =>
        turn(`a${n}`, n, {
          status: next(3) === 0 ? 'running' : 'done',
          startedBy: Array.from({length: next(3)}, () => `a${next(size + 2)}`),
        }),
      );
      const open = rowsOf(spans, ['', ...spans.map((_, n) => `a${n}`)]);
      expect(names(open).sort()).toEqual(spans.map((_, n) => `a${n}`).sort());
      // folded: what is shown plus what folds away is every agent
      const rows = rowsOf(spans);
      const shown = rows.filter(row => row.kind === 'agent').length;
      const hidden = rows.reduce((sum, row) => sum + (row.kind === 'fold' ? row.count : 0), 0);
      expect(shown + hidden).toBe(size);
    }
  });

  test('the headline shows only when it says more than the role', () => {
    const rows = rowsOf([
      turn('a', 1, {role: 'planner', title: 'planner'}),
      turn('b', 2, {role: 'planner', title: 'Padded the ring'}),
    ]);
    expect(rows.map(row => (row.kind === 'agent' ? row.headline : null))).toEqual([
      null,
      'Padded the ring',
    ]);
  });
});

describe('the selected agent', () => {
  const finished = (parent: string, prefix: string): Span[] =>
    Array.from({length: FOLD_FROM}, (_, n) => turn(`${prefix}${n}`, 10 + n, {startedBy: [parent]}));

  test('is never folded away, nor are its ancestors, so its group stays shown', () => {
    const spans = [
      turn('boss', 1, {status: 'running'}),
      ...finished('boss', 'old'),
      turn('mid', 20, {startedBy: ['boss']}),
      ...finished('mid', 'kid'),
    ];
    // Unselected, the finished children fold under boss.
    expect(names(rowsOf(spans))).toContain(`+${FOLD_FROM + FOLD_FROM + 1}`);
    const rows = names(rowsOf(spans, [], 'kid2'));
    expect(rows).toContain('kid2');
    expect(rows).toContain('mid');
    expect(rows).toContain('boss');
  });

  test('is drawn whatever the run looks like, as every agent is selectable', () => {
    let seed = 11;
    const next = (bound: number): number => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % bound;
    };
    for (let run = 0; run < 50; run++) {
      const size = 1 + next(30);
      const spans = Array.from({length: size}, (_, n) =>
        turn(`a${n}`, n, {
          status: next(4) === 0 ? 'running' : 'done',
          startedBy: n === 0 ? [] : [`a${next(n)}`],
        }),
      );
      for (let pick = 0; pick < size; pick++) {
        expect(names(rowsOf(spans, [], `a${pick}`))).toContain(`a${pick}`);
      }
    }
  });
});

describe('agentModel', () => {
  const agentOf = (model: string | null) =>
    agentGraph([turn('a', 1, {role: 'implementer'})]).agents.map(agent => ({...agent, model}))[0];
  const running = (agentKind: string, model: string | null) => ({
    ...initialCoreState(),
    activeExecutions: {
      x: {
        executionId: 'x',
        agentKind,
        roundLabel: null,
        roundNumber: null,
        roundKey: null,
        stage: 's',
        attempt: null,
        assignment: '',
        startedAt: '',
        activity: {mode: 'thinking' as const, summary: ''},
        model,
      },
    },
  });

  test('is the model the turn names', () => {
    const agent = agentOf('turn-model');
    expect(agent && agentModel(agent, running('implementer', 'other'))).toBe('turn-model');
  });

  test('is the resolved model of a running execution of its role when the turn names none', () => {
    const agent = agentOf(null);
    expect(agent && agentModel(agent, running('implementer', 'gpt-6-luna'))).toBe('gpt-6-luna');
    expect(agent && agentModel(agent, running('judge', 'gpt-6-luna'))).toBeNull();
    expect(agent && agentModel(agent, null)).toBeNull();
  });
});

describe('agentTurns', () => {
  test('lists the turns of one agent in order with their headlines', () => {
    const spans = [
      turn('a', 1, {title: 'first'}),
      turn('b', 2, {title: 'other'}),
      turn('a', 3, {title: 'second', status: 'running'}),
    ];
    const turns = agentTurns(new Map(spans.map(span => [span.id, span])), 'a');
    expect(turns.map(item => [item.number, item.headline, item.status, item.endedAt])).toEqual([
      [1, 'first', 'done', 2],
      [2, 'second', 'running', null],
    ]);
    expect(turns.map(item => item.span)).toEqual(['turn:a:1', 'turn:a:3']);
  });
});
