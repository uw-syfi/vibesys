import {describe, expect, test} from 'bun:test';
import type {RunEvent, Span} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {createRunReader, type RunModel} from './run-model.js';
import {runPageInitial, runPageStep} from './run-page.js';
import {type TurnNarrative, turnNarrative} from './step-narrative.js';
import {type ResultContext, type TurnResultView, turnResult} from './turn-result.js';
import {viewedRun} from './views.js';

/**
 * The card a turn ends with, read from the events the server recorded: the run has two ideas (the
 * second builds on the first), each with an orchestrator turn, and the tests read the reply of the
 * turn they open.
 */

function span(id: string, parent: string | null, extra: Partial<Span>): Span {
  return {
    id,
    parent,
    type: parent === null ? 'flow' : 'operation',
    name: id,
    status: 'done',
    started_at: 1,
    ended_at: 101,
    ...extra,
  } as Span;
}

function workstream(id: string, title: string, round: number, base: number | null): Span {
  return span(id, 'flow:root#1', {
    label: {
      kind: 'workstream',
      title,
      attrs_json: JSON.stringify({hypothesis_id: `h${round}`, round, base_round: base}),
      links: base === null || base === 0 ? [] : [{rel: 'builds_on', to: `ws${base}`}],
    },
  } as Partial<Span>);
}

function turnSpan(id: string, parent: string, status: Span['status']): Span {
  return span(id, parent, {
    status,
    label: {
      kind: 'agent_turn',
      title: 'orchestrator',
      attrs_json: '{"role":"orchestrator"}',
      links: [],
    },
  } as Partial<Span>);
}

const PLAN_SCHEMA = {
  properties: {
    hypothesis_id: {type: 'string'},
    hypothesis_updates: {type: 'array', items: {type: 'object'}},
    roadmap: {type: 'array', items: {type: 'object'}},
    hypothesis: {type: 'string'},
    title: {type: 'string'},
    activation_evidence: {type: 'string'},
    falsification_criteria: {type: 'string'},
    expected_effect: {type: 'string'},
    minimum_acceptance_criteria: {type: 'string'},
    invariants: {type: 'string'},
    recommended_skills: {type: 'array', items: {type: 'object'}},
    task: {type: 'string'},
    pass_criteria: {type: 'string'},
    base_round: {anyOf: [{type: 'integer'}, {type: 'null'}]},
    reasoning: {type: 'string'},
  },
  type: 'object',
};

const PLAN = {
  hypothesis_id: 'fixed-slot-ring',
  hypothesis_updates: [{hypothesis_id: 'pad', disposition: 'parked', reason: 'Inside the noise.'}],
  roadmap: [{id: 'a', kind: 'major', title: 'Lock-free handoff', status: 'todo', note: ''}],
  hypothesis: 'A node per push puts the allocator on the hot path; a ring of slots removes it.',
  title: 'Preallocate fixed-size queue slots',
  activation_evidence: 'Slot reuse counter above zero.',
  falsification_criteria: 'No gain at eight producers.',
  expected_effect: '1.3x to 1.6x more pushes per second.',
  minimum_acceptance_criteria: '1.1x with no test regression.',
  invariants: 'FIFO order per producer.',
  recommended_skills: [],
  task: 'Replace the node allocator with a ring.',
  pass_criteria: 'All queue tests pass and the benchmark improves.',
  base_round: null as number | null,
  reasoning: 'The first idea shows allocation dominates the profile.',
};

function replyEvent(sequence: number, span_id: string, value: Record<string, unknown>): RunEvent {
  return event(sequence, 'agent_execution_finished', {
    invocation_id: `inv-${span_id}`,
    agent_kind: 'orchestrator',
    span_id,
    data: {
      kind: 'agent_execution_finished',
      result: '{}',
      reply: {kind: 'OrchestratorPlan', json_schema: PLAN_SCHEMA, value},
    },
  } as never);
}

function chunk(sequence: number, span_id: string, content: string): RunEvent {
  return event(sequence, 'agent_output_chunk', {
    invocation_id: `inv-${span_id}`,
    agent_kind: 'orchestrator',
    span_id,
    data: {kind: 'agent_output_chunk', channel: 'analysis', content},
  });
}

function tool(sequence: number, span_id: string): RunEvent {
  return event(sequence, 'tool_call', {
    invocation_id: `inv-${span_id}`,
    agent_kind: 'orchestrator',
    span_id,
    data: {kind: 'tool_call', tool: 'Read', call_id: 'c1', args: {path: 'src/queue.rs'}},
  });
}

interface World {
  readonly model: RunModel;
  readonly narrative: (span: string) => TurnNarrative | null;
  readonly context: (idea: number | null) => ResultContext;
}

function world(transcript: readonly RunEvent[], status: Span['status'] = 'done'): World {
  const spans = [
    span('flow:root#1', null, {name: 'dynamic'}),
    workstream('ws1', 'Pad the counters', 1, null),
    turnSpan('turn1', 'ws1', 'done'),
    workstream('ws2', 'Preallocate fixed-size queue slots', 2, 1),
    turnSpan('turn2', 'ws2', status),
  ];
  let state = runPageInitial({
    identity: {host: 'ssh:host-b', runId: '20261015-163047'},
    title: {project: '~/p', task: 'mpmc'},
    hostLabel: 'host-b',
    unavailable: {},
  });
  state = {...state, link: {kind: 'attached', instance: 'a1b2c3d4e5f6'}};
  state = runPageStep(state, {
    type: 'run-data',
    wallMs: 0,
    data: {
      kind: 'events',
      events: spans.map((each, index) => ({
        ...event(index + 1, 'span_started'),
        data: {kind: 'span_started', span: each},
      })) as RunEvent[],
    },
  }).state;
  state = runPageStep(state, {
    type: 'run-core',
    core: reduceEventBatch(initialCoreState(), transcript),
  }).state;
  const viewed = viewedRun(state);
  const model = createRunReader().read(state.run.trace, null);
  return {
    model,
    narrative: id => turnNarrative(viewed.trace, viewed.core, id),
    context: idea => ({
      candidates: model.candidates.candidates,
      idea: model.candidates.candidates.find(each => each.round === idea) ?? null,
    }),
  };
}

function resultOf(w: World, span: string, idea: number | null): TurnResultView | null {
  const last = w.narrative(span)?.blocks.at(-1);
  return last?.kind === 'reply' ? turnResult(last.reply, w.context(idea)) : null;
}

describe('the plan an orchestrator turn ends with', () => {
  const recorded = [
    chunk(10, 'turn2', '**Reading**\n\nThe profile is allocation bound.'),
    tool(11, 'turn2'),
    chunk(12, 'turn2', '**Structuring roadmap criteria**'),
    replyEvent(13, 'turn2', PLAN),
  ];

  test('reads title, base, hypothesis, effect, reasoning and criteria in that order', () => {
    const w = world(recorded);
    const card = resultOf(w, 'turn2', 2);
    expect(card?.label).toBe('Plan');
    expect(card?.headline).toBe('Preallocate fixed-size queue slots');
    expect(card?.chips).toEqual([]);
    expect(card?.sections.map(each => each.key)).toEqual([
      'hypothesis',
      'expected_effect',
      'reasoning',
      'pass_criteria',
      'falsification_criteria',
      'hypothesis_updates',
      'roadmap',
    ]);
    const updates = card?.sections.find(each => each.key === 'hypothesis_updates');
    expect(updates).toEqual({
      kind: 'list',
      key: 'hypothesis_updates',
      label: 'Updates to other hypotheses',
      items: ['pad: parked. Inside the noise.'],
    });
    const roadmap = card?.sections.find(each => each.key === 'roadmap');
    expect(roadmap?.kind === 'list' && roadmap.items).toEqual(['Lock-free handoff (todo)']);
  });

  test('lists the less useful fields after the body, and none of them in the body', () => {
    const card = resultOf(world(recorded), 'turn2', 2);
    expect(card?.more.map(each => each.key)).toEqual([
      'hypothesis_id',
      'activation_evidence',
      'minimum_acceptance_criteria',
      'invariants',
      'task',
    ]);
    const shown = new Set(card?.sections.map(each => each.key));
    for (const field of card?.more ?? []) expect(shown.has(field.key)).toBe(false);
  });

  test('the base is the input, or the idea the plan names, which opens', () => {
    const w = world(recorded);
    const first = world([
      ...recorded.slice(0, 3),
      replyEvent(13, 'turn2', {...PLAN, base_round: 0}),
    ]);
    expect(resultOf(first, 'turn2', 2)?.builtOn).toEqual({label: 'input', idea: null});
    const named = world([
      ...recorded.slice(0, 3),
      replyEvent(13, 'turn2', {...PLAN, base_round: 1}),
    ]);
    expect(resultOf(named, 'turn2', 2)?.builtOn).toEqual({label: 'Pad the counters', idea: 'ws1'});
    // A plan that names no base builds on what its idea recorded: the first idea, the input.
    expect(resultOf(w, 'turn2', 1)?.builtOn).toEqual({label: 'input', idea: null});
    expect(resultOf(w, 'turn2', 2)?.builtOn).toEqual({label: 'Pad the counters', idea: 'ws1'});
    expect(resultOf(w, 'turn2', null)?.builtOn).toBeNull();
  });

  test('a turn of another role (a judge) builds on nothing, whatever its reply carries', () => {
    const w = world(recorded);
    const last = w.narrative('turn2')?.blocks.at(-1);
    if (last?.kind !== 'reply') throw new Error('no reply');
    const judged = turnResult({...last.reply, author: 'Reviewer', role: 'judge'}, w.context(2));
    expect(judged.builtOn).toBeNull();
    expect(judged.summary).not.toContain('builds on');
  });

  test('leads the turn with one line: the title and what it builds on', () => {
    const w = world(recorded);
    expect(resultOf(w, 'turn2', 2)?.summary).toBe(
      'Plan: Preallocate fixed-size queue slots · builds on Pad the counters',
    );
    expect(resultOf(w, 'turn2', 1)?.summary).toBe(
      'Plan: Preallocate fixed-size queue slots · builds on input',
    );
  });

  test('is the last block of the narrative, after the last thought and tool call', () => {
    const w = world([...recorded, chunk(14, 'turn2', 'Late thought.')]);
    const blocks = w.narrative('turn2')?.blocks ?? [];
    expect(blocks.map(each => each.kind)).toEqual([
      'thought',
      'tools',
      'thought',
      'thought',
      'reply',
    ]);
  });

  test('is absent while the turn runs with no reply', () => {
    const w = world(recorded.slice(0, 3), 'running');
    expect(w.narrative('turn2')?.blocks.map(each => each.kind)).toEqual([
      'thought',
      'tools',
      'thought',
    ]);
    expect(resultOf(w, 'turn2', 2)).toBeNull();
  });

  test('shows the agent’s long text in full', () => {
    const long = 'Reasons. '.repeat(200).trim();
    const w = world([...recorded.slice(0, 3), replyEvent(13, 'turn2', {...PLAN, reasoning: long})]);
    const reasoning = resultOf(w, 'turn2', 2)?.sections.find(each => each.key === 'reasoning');
    expect(reasoning?.kind === 'prose' && reasoning.text).toBe(long);
  });
});

describe('another role’s reply keeps its fields and ends the turn', () => {
  function implementer(value: Record<string, unknown>): RunEvent {
    return event(13, 'agent_execution_finished', {
      invocation_id: 'inv',
      agent_kind: 'implementer',
      span_id: 'turn2',
      data: {
        kind: 'agent_execution_finished',
        result: '{}',
        reply: {
          kind: 'ImplementerResponse',
          json_schema: {
            properties: {
              summary: {type: 'string'},
              changed: {type: 'array', items: {type: 'string'}},
              outcome: {enum: ['done', 'blocked'], type: 'string'},
            },
            type: 'object',
          },
          value,
        },
      },
    } as never);
  }

  test('puts its choice on a chip, its summary in the body, the rest after it', () => {
    const w = world([
      chunk(10, 'turn2', 'Working.'),
      implementer({
        summary: 'Padded the counters.\nSecond line.',
        changed: ['a.rs'],
        outcome: 'done',
      }),
    ]);
    const card = resultOf(w, 'turn2', 2);
    expect(card?.label).toBe('Builder');
    expect(card?.headline).toBeNull();
    expect(card?.chips).toEqual([{label: 'done'}]);
    expect(card?.sections.map(each => each.key)).toEqual(['summary']);
    expect(card?.more.map(each => each.key)).toEqual(['changed']);
    expect(card?.summary).toBe('Builder response: done · Padded the counters.');
  });
});
