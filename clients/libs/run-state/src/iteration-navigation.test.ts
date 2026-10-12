import {describe, expect, test} from 'bun:test';
import type {IterationPage} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {
  initialIterationNavigation,
  iterationChildren,
  iterationRoots,
  reduceIterationResult,
} from './iteration-navigation.js';
import {reduceEvent} from './reduce.js';
import {initialCoreState} from './state.js';

function page(overrides: Partial<IterationPage> = {}): IterationPage {
  return {
    kind: 'page',
    schema_version: 1,
    run_id: 'run-1',
    projection_revision: 'revision-1',
    parent_id: null,
    from_cursor: null,
    items: [],
    next_cursor: null,
    ...overrides,
  };
}

const item = {
  id: 'generation:0',
  parent_id: null,
  kind: 'generation' as const,
  label: 'Generation 0',
  ordinal: 0,
  status: 'completed' as const,
  child_count: 0,
  headline_metrics: [],
};

describe('iteration navigation', () => {
  test('normalizes consecutive pages by stable id', () => {
    const first = reduceIterationResult(
      initialIterationNavigation(),
      page({items: [item], next_cursor: 'next'}),
    );
    const second = reduceIterationResult(
      first,
      page({
        from_cursor: 'next',
        items: [{...item, id: 'generation:1', label: 'Generation 1', ordinal: 1}],
      }),
    );

    expect(iterationChildren(second, null).map(node => node.id)).toEqual([
      'generation:0',
      'generation:1',
    ]);
  });

  test('rejects an out-of-order continuation', () => {
    expect(() =>
      reduceIterationResult(initialIterationNavigation(), page({from_cursor: 'unknown'})),
    ).toThrow('different projection revision');
  });

  test('accepts an identical continuation replay without duplicating rows', () => {
    const first = reduceIterationResult(
      initialIterationNavigation(),
      page({items: [item], next_cursor: 'next'}),
    );
    const continuation = page({
      from_cursor: 'next',
      items: [{...item, id: 'generation:1', label: 'Generation 1', ordinal: 1}],
    });
    const applied = reduceIterationResult(first, continuation);

    expect(reduceIterationResult(applied, continuation)).toBe(applied);
    expect(iterationChildren(applied, null).map(node => node.id)).toEqual([
      'generation:0',
      'generation:1',
    ]);
  });

  test('rejects a conflicting replay at an already applied cursor', () => {
    const first = reduceIterationResult(
      initialIterationNavigation(),
      page({items: [item], next_cursor: 'next'}),
    );
    const applied = reduceIterationResult(
      first,
      page({
        from_cursor: 'next',
        items: [{...item, id: 'generation:1', label: 'Generation 1', ordinal: 1}],
      }),
    );

    expect(() =>
      reduceIterationResult(
        applied,
        page({
          from_cursor: 'next',
          items: [{...item, id: 'generation:2', label: 'Generation 2', ordinal: 2}],
        }),
      ),
    ).toThrow('replay conflicts');
  });

  test('replaces stale normalized state at a new revision', () => {
    const old = reduceIterationResult(initialIterationNavigation(), page({items: [item]}));
    const next = reduceIterationResult(old, page({projection_revision: 'revision-2', items: []}));

    expect(iterationChildren(next, null)).toEqual([]);
    expect(next.revision).toBe('revision-2');
  });

  test('keeps an event-derived active round beside persisted queried rounds', () => {
    const core = reduceEvent(
      initialCoreState(),
      event(1, 'agent_execution_started', {
        execution_id: 'impl-2',
        agent_kind: 'implementer',
        round_label: 'round-2',
        status: 'active',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement round two',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Working',
            tool: null,
          },
        },
      }),
    );
    const navigation = reduceIterationResult(
      initialIterationNavigation(),
      page({
        items: [
          {
            ...item,
            id: 'round:1',
            kind: 'round',
            label: 'Round 1',
            ordinal: 1,
          },
        ],
      }),
    );

    expect(iterationRoots(navigation, core)).toMatchObject([
      {id: 'round:1', status: 'completed'},
      {id: 'round:2', status: 'active'},
    ]);
  });

  test('retains event-derived roots while a supported projection is initially empty', () => {
    const core = reduceEvent(
      initialCoreState(),
      event(1, 'agent_execution_started', {
        execution_id: 'impl-1',
        agent_kind: 'implementer',
        round_label: 'round-1',
        status: 'active',
        data: {
          kind: 'agent_execution_started',
          stage: 'implementation',
          attempt: 1,
          system_prompt: '',
          user_prompt: 'Implement round one',
          activity: {
            kind: 'agent_execution_activity_changed',
            mode: 'thinking',
            summary: 'Working',
            tool: null,
          },
        },
      }),
    );
    const navigation = reduceIterationResult(initialIterationNavigation(), page());

    expect(iterationRoots(navigation, core)).toMatchObject([{id: 'round:1', status: 'active'}]);
  });
});
