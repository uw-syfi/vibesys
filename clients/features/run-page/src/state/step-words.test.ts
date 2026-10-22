import {describe, expect, test} from 'bun:test';
import type {Span} from '@vibesys/contracts';
import {stepName} from './span-reading.js';
import {adoptionTitles, openWaits, titleOf, verdictWords} from './span-words.js';

/**
 * How the plumbing around a search's labelled steps reads: operations and flows the search did not
 * label are named by the runtime's registry, and the words are the client's. A step that waits says
 * what it waits on, and only the stop that was asked for.
 */

const STRATEGIES = ['multi', 'single', 'dynamic', 'evolve', 'plain'] as const;
const OPERATIONS = [
  'render_role_artifacts',
  'interpret_evidence',
  'verify_parent_revision',
  'capture_command',
  'validate_local',
  'export_patch',
  'write_handoff',
] as const;
const SHARED = ['roadmap.read', 'roadmap.put_items', 'agent.turn', 'play_round'] as const;

function span(id: string, parent: string | null, extra: Partial<Span> = {}): Span {
  return {
    id,
    parent,
    type: parent === null || id.startsWith('flow') ? 'flow' : 'operation',
    name: id,
    status: 'running',
    waiting_on: [],
    attempts: [],
    started_at: 0,
    ended_at: null,
    failure: null,
    stop: null,
    ...extra,
  } as Span;
}

describe('the runtime steps no search labels', () => {
  test('read in words, never as a registered name', () => {
    const names = [
      ...STRATEGIES.flatMap(strategy => OPERATIONS.map(operation => `${strategy}.${operation}`)),
      ...SHARED,
    ];
    const titles = names.map((name, index) => titleOf(span(`op:${index}`, 'flow:root#1', {name})));
    for (const title of titles) expect(title).not.toMatch(/[._]/);
    expect(titles[0]).toBe('Writing the agent’s instructions');
    expect(titles).toContain('Reading the roadmap');
    expect(titles).toContain('Play round');
  });

  test('a name the registry does not list reads as it is', () => {
    expect(stepName('measure_the_input')).toBeNull();
    expect(stepName('multi.something_new')).toBeNull();
    expect(titleOf(span('op:x', 'flow:root#1', {name: 'measure_the_input'}))).toBe(
      'measure_the_input',
    );
  });
});

describe('a step that waits', () => {
  const root = span('flow:root#1', null, {
    name: 'multi',
    waiting_on: [
      {kind: 'slot', ref: 'group', since: 5},
      {kind: 'stop', ref: 'stop', since: 5},
    ],
  });

  test('waits on its steps, not the stop nobody asked for', () => {
    expect(openWaits(root).map(wait => wait.kind)).toEqual(['slot']);
  });

  test('waits on the stop once it was asked for', () => {
    const asked = {...root, stop: {requested_at: 9, landed: false}} as Span;
    expect(openWaits(asked).map(wait => wait.kind)).toEqual(['slot', 'stop']);
  });

  test('waits on nothing once it ended', () => {
    const done = {...root, status: 'done', ended_at: 12} as Span;
    expect(openWaits(done)).toEqual([]);
  });
});

describe('an adoption step', () => {
  const adoption = (attrs: object, verdict: string) =>
    span('op:adopt', 'flow:root#1', {
      name: 'adopt',
      label: {
        kind: 'adoption',
        title: 'abc1234def5678',
        attrs_json: JSON.stringify(attrs),
        verdict,
        links: [],
      },
    } as Partial<Span>);

  test('reads as adopting the candidate, by a short revision, or the input', () => {
    const adopted = adoption({candidate: 'abc1234def5678'}, 'adopted');
    expect(titleOf(adopted)).toBe('Adopt candidate abc1234');
    expect(verdictWords(adopted)?.label).toBe('Adopted');
    const input = adoption({}, 'not_adopted');
    expect(titleOf(input)).toBe('Adopt the input');
    expect(verdictWords(input)?.label).toBe('Not adopted');
  });

  test('shows the recorded checkout skip', () => {
    const dirty = adoption({candidate: 'abc1234def5678', checkout_status: 'dirty_tree'}, 'adopted');
    expect(titleOf(dirty)).toBe('Adopt candidate abc1234 · Dirty tree');
    const changed = adoption(
      {checkout_status: 'CHECKOUT_STATUS_OPERATOR_CHANGED_CHECKOUT'},
      'not_adopted',
    );
    expect(titleOf(changed)).toBe('Adopt the input · Operator changed checkout');
  });

  test('projects that headline by span id and leaves other spans out', () => {
    const dirty = adoption({candidate: 'abc1234def5678', checkout_status: 'dirty_tree'}, 'adopted');
    const turn = span('turn', 'flow:root#1', {name: 'implementer'});
    expect(
      adoptionTitles(
        new Map([
          [dirty.id, dirty],
          [turn.id, turn],
        ]),
      ),
    ).toEqual(new Map([['op:adopt', 'Adopt candidate abc1234 · Dirty tree']]));
    expect(adoptionTitles(undefined).size).toBe(0);
  });
});

describe('an agent turn', () => {
  test('a scripted turn does not show a configured model', () => {
    const turn = span('turn', 'flow:root#1', {
      name: 'implementer',
      label: {
        kind: 'agent_turn',
        title: 'implementer',
        attrs_json: JSON.stringify({model: 'configured-model', client: 'scripted'}),
        links: [],
      },
    } as Partial<Span>);
    expect(titleOf(turn)).toBe('Implementer');
  });
});

describe('a gate step', () => {
  test('is titled by its stages, a build stage as the candidate build', () => {
    const gate = span('op:gate', 'flow:root#1', {
      name: 'measure',
      label: {kind: 'gate', title: 'build + benchmark', attrs_json: '{}', links: []},
    } as Partial<Span>);
    expect(titleOf(gate)).toBe('Gate · candidate build + benchmark');
  });
});
