import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch, reduceEventPrefix} from '@vibesys/core-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {agentGraph} from './agents.js';
import {activityRound, needsOlder} from './derive.js';
import {statusLine} from './rounds.js';
import {CAPTURED_TYPES} from './session.js';
import {roundTranscript, toolDetail} from './transcript.js';
import {Agents} from './ui/Agents.js';
import {Transcript} from './ui/Transcript.js';
import {INITIAL_UI, uiReducer} from './ui-state.js';

const event = (sequence: number, fields: Partial<RunEvent>): RunEvent => ({
  sequence,
  type: 'agent_execution_started',
  timestamp: `2026-09-30T02:19:${String(sequence).padStart(2, '0')}Z`,
  agent_kind: 'orchestrator',
  round_label: 'orchestrator-session-turn-1',
  execution_id: 'design',
  invocation_id: 'design',
  ...fields,
});
const preparation: RunEvent[] = [
  event(1, {type: 'run_started'}),
  event(2, {
    status: 'active',
    data: {
      kind: 'agent_execution_started',
      stage: 'orchestrator',
      user_prompt: 'Inspect the render pipeline.',
      activity: {kind: 'agent_execution_activity_changed', mode: 'thinking', summary: 'Thinking'},
    },
  }),
  event(3, {
    type: 'agent_output_chunk',
    data: {kind: 'agent_output_chunk', channel: 'assistant', content: 'Inspecting the pipeline.'},
  }),
  event(4, {
    type: 'tool_call',
    data: {kind: 'tool_call', call_id: 'read', tool: 'Read', args: {file_path: 'src/render.py'}},
  }),
  event(5, {
    type: 'agent_execution_activity_changed',
    data: {
      kind: 'agent_execution_activity_changed',
      mode: 'tool',
      summary: 'Using Read',
      tool: 'Read',
    },
  }),
];

function view(events: RunEvent[], round: number | null = null) {
  const core = reduceEventBatch(initialCoreState(), events);
  const model = roundTranscript({
    core,
    captured: events.filter(item => CAPTURED_TYPES.has(item.type)),
    sent: [],
    round,
    runId: null,
  });
  const html = renderToStaticMarkup(
    <Transcript
      round={round}
      row={null}
      result={[]}
      model={model}
      follow={false}
      history={{loading: false, error: null, onRetry: () => {}}}
      empty="Waiting for round 1."
      endline={null}
      controls={{
        expanded: null,
        disclosed: {},
        detail: id => toolDetail(core, id),
        onExpand: () => {},
        onDisclose: () => {},
      }}
      only={null}
      onShowAll={() => {}}
    />,
  );
  return {core, model, html};
}

test('unscoped preparation shows its real agent output and tools before any round exists', () => {
  const {core, model, html} = view(preparation);
  assert.deepEqual(core.rounds, []);
  assert.equal(model.turns[0]?.active, true);
  assert.match(html, /Run activity/);
  assert.match(html, /Inspecting the pipeline\./);
  assert.match(html, /src\/render\.py/);
  assert.equal(html.includes('Waiting for round 1.'), false);
  assert.equal(statusLine(core, null).text, 'Orchestrator: Using Read');
  assert.deepEqual(
    agentGraph(core, null).nodes.map(node => [node.id, node.status]),
    [['design', 'active']],
  );
  const agents = renderToStaticMarkup(
    <Agents
      round={null}
      graph={agentGraph(core, null)}
      selected={null}
      width={400}
      onSelect={() => {}}
      detail={null}
    />,
  );
  assert.match(agents, /Run activity agent invocations/);
});

for (const status of ['completed', 'failed'] as const) {
  test(`unscoped ${status} execution stays readable after a run ends without rounds`, () => {
    const {core, model, html} = view([
      ...preparation,
      event(6, {
        type: 'agent_execution_finished',
        status,
        data: {
          kind: 'agent_execution_finished',
          error: status === 'failed' ? 'Design validation failed.' : null,
        },
      }),
      event(7, {type: status === 'failed' ? 'run_failed' : 'run_finished', status}),
    ]);
    assert.deepEqual(core.rounds, []);
    assert.equal(model.turns[0]?.active, false);
    assert.match(html, /Inspecting the pipeline\./);
    assert.match(html, status === 'completed' ? /Completed/ : /Failed/);
    if (status === 'failed') assert.match(html, /Design validation failed\./);
    assert.deepEqual(
      agentGraph(core, null).nodes.map(node => node.status),
      [status],
    );
  });
}

test('unscoped calls stay separate and readable after numeric round history appears', () => {
  const events = [
    ...preparation,
    event(6, {type: 'round_finished', round_label: 'round-1', status: 'completed'}),
    event(7, {
      type: 'agent_output_chunk',
      data: {
        kind: 'agent_output_chunk',
        channel: 'assistant',
        content: 'Planning the next change.',
      },
    }),
  ];
  const activity = view(events);
  assert.equal(activity.core.rounds[0]?.number, 1);
  assert.equal(activityRound(activity.core), null);
  assert.match(activity.html, /Planning the next change\./);
  assert.equal(view(events, 1).html.includes('Planning the next change.'), false);
});

test('following activity moves between real numeric and run scopes without assigning calls to a round', () => {
  const data = preparation[1]?.data;
  assert.ok(data);
  const numbered = event(6, {
    round_label: 'round-1-plan',
    execution_id: 'numbered',
    invocation_id: 'numbered',
    data,
  });
  const before = view([...preparation, numbered], 1);
  assert.equal(activityRound(before.core), 1);
  assert.deepEqual(
    before.model.turns.map(turn => turn.id),
    ['numbered'],
  );
  const after = view([
    ...preparation,
    numbered,
    event(7, {execution_id: 'next', invocation_id: 'next', data}),
  ]);
  assert.equal(activityRound(after.core), null);
  assert.deepEqual(
    after.model.turns.map(turn => turn.id),
    ['design', 'next'],
  );
});

test('explicit run activity is distinct from following a numeric live scope', () => {
  const selected = uiReducer(INITIAL_UI, {type: 'round', round: null, live: 1});
  assert.equal(selected.round, null);
  assert.notEqual(selected.round, INITIAL_UI.round);
  assert.equal(uiReducer(selected, {type: 'round', round: null, live: null}).round, 'live');
});

test('the latest real start wins over the storage order of seeded numeric role slots', () => {
  const data = preparation[1]?.data;
  assert.ok(data);
  const core = reduceEventBatch(initialCoreState(), [
    event(1, {
      type: 'run_started',
      agent_kind: null,
      round_label: null,
      execution_id: null,
      invocation_id: null,
      data: {
        kind: 'run_started',
        input: 'Inspect the render pipeline.',
        expected_roles: ['orchestrator', 'implementer'],
        outer_loop: 'agent',
      },
    }),
    event(2, {round_label: 'round-1-plan', execution_id: 'plan', invocation_id: 'plan', data}),
    event(3, {execution_id: 'unscoped', invocation_id: 'unscoped', data}),
    event(4, {
      agent_kind: 'implementer',
      round_label: 'round-1-implementer',
      execution_id: 'implement',
      invocation_id: 'implement',
      data,
    }),
  ]);
  assert.equal(activityRound(core), 1);
  assert.equal(statusLine(core, null).text, 'Implementing round 1');
});

test('run activity requests its hidden history until a public tail bootstrap is fully backfilled', () => {
  const events = [
    ...preparation,
    event(6, {type: 'control', status: 'pending', text: '/steer: Measure frame time.'}),
    event(7, {type: 'control', status: 'consumed', text: '/steer'}),
    event(8, {
      type: 'agent_output_chunk',
      data: {kind: 'agent_output_chunk', channel: 'assistant', content: 'Measuring frame time.'},
    }),
  ];
  const tail = reduceEventBatch(initialCoreState(), events.slice(6), undefined, 8, 6);
  assert.equal(needsOlder(tail, null), true);
  const full = reduceEventPrefix(tail, events.slice(0, 6), 0);
  assert.equal(needsOlder(full, null), false);
  const model = roundTranscript({
    core: full,
    captured: events,
    sent: [],
    round: null,
    runId: null,
  });
  assert.equal(model.turns[0]?.prompt, 'Inspect the render pipeline.');
  assert.ok(model.turns[0]?.items.some(item => item.kind === 'prose'));
  assert.ok(
    model.turns[0]?.items.some(
      item => item.kind === 'steer' && item.text === 'Measure frame time.',
    ),
  );
});
