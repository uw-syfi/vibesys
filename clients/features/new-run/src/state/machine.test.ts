import {describe, expect, test} from 'bun:test';
import type {Check, ExampleList, MachineDescription} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';
import type {HarnessOffer, Offered} from '@vibesys/ipc';
import {checkMachine} from '@vibesys/machine/testing';
import type {RecentRun} from '@vibesys/runs';
import type {Seeded} from '@vibesys/store/testing';
import {createActor, fromPromise, type SnapshotFrom} from 'xstate';
import {
  checksPass,
  inputsKey,
  type NewRunEvent,
  newRunMachine,
  ROLES,
  runInputs,
  vettedModels,
} from './index.js';

const OFFERS: readonly HarnessOffer[] = [
  {provider: 'claude', model: 'claude-a', default: true},
  {provider: 'claude', model: 'claude-b', default: false},
  {provider: 'codex', model: 'gpt-a', default: true},
];
const RECENT: readonly RecentRun[] = [
  {
    host: 'local',
    hostLabel: 'This Mac',
    project: '/p/queue',
    task: 'mpmc',
    runId: 'r1',
    instanceId: null,
    attachedAt: 2,
  },
  {
    host: 'local',
    hostLabel: 'This Mac',
    project: '/p/kv',
    task: null,
    runId: 'r0',
    instanceId: null,
    attachedAt: 1,
  },
  {
    host: 'ssh:gpu',
    hostLabel: 'gpu',
    project: '/q',
    task: 'spsc',
    runId: 'r2',
    instanceId: null,
    attachedAt: 3,
  },
];
const IDS = [
  'checkout_found',
  'project_own_repository',
  'settings_valid',
  'docker_reachable',
  'agent_image',
  'evaluator_available',
] as const;

function checks(random: Seeded): Check[] {
  return IDS.map(id => ({
    id,
    state: random.pick(['ok', 'ok', 'warn', 'fail'] as const),
    detail: id,
  }));
}

/** What each operation answers: plausible values, chosen at random. */
function output(operation: string, random: Seeded): unknown {
  switch (operation) {
    case 'describe':
    case 'examples':
      return random.pick([{kind: 'unsupported'}, {kind: 'offered', value: {}}]);
    case 'harnesses':
      return OFFERS;
    case 'tasks':
      return random.pick([['mpmc', 'spsc'], ['spsc'], []]);
    case 'preflight':
      return {kind: 'offered', value: checks(random)};
    case 'fix':
      return {project: random.pick(['/own/a', '/own/b']), tasks: ['mpmc']};
    default:
      return {runId: 'run-new', instanceId: '0123456789ab'};
  }
}

function inputEvent(random: Seeded, snapshot: SnapshotFrom<typeof newRunMachine>): NewRunEvent {
  const agent = snapshot.context.form.agent;
  return random.pick<NewRunEvent>([
    {type: 'machine', machine: random.pick(['local', 'ssh:gpu'])},
    {type: 'project', project: {source: 'folder', path: random.pick(['/p/queue', '/p/kv'])}},
    {type: 'project', project: {source: 'example', example: 'queue-rs', destination: '~/queue-rs'}},
    {type: 'task', task: random.pick(['mpmc', 'spsc'])},
    {type: 'provider', provider: random.pick(['claude', 'codex'])},
    {type: 'model', model: random.pick(vettedModels(OFFERS, agent?.provider ?? 'claude'))},
    {
      type: 'role',
      role: {role: random.pick(ROLES).role, model: null, effort: random.pick(['default', 'high'])},
    },
    {type: 'budget', budget: {rounds: random.pick([1, 2, 5]), timeLimitS: null, results: 'keep'}},
    {type: 'advanced', advanced: random.pick(['', '--max-in-flight 3', '--max-rounds 4', '"open'])},
    {type: 'recheck'},
    {type: 'fix', fix: {kind: 'copy_out', source: '/p/queue', destination: '/own/a'}},
    {type: 'start'},
  ]);
}

type Snapshot = SnapshotFrom<typeof newRunMachine>;

/** Start is entered only from inputs whose checks are current and pass. */
function expectStartOnlyWhenChecksPass(before: Snapshot, after: Snapshot): void {
  const starting = (snapshot: Snapshot) => snapshot.matches({editing: {action: 'starting'}});
  if (!starting(before) && starting(after)) {
    expect(checksPass(before.context.form, before.context.checks)).toBe(true);
  }
}

const INPUTS = ['project', 'task', 'provider', 'model', 'role', 'budget', 'advanced'];

/** An input other than the machine never restarts what the machine offers. */
function expectInputKeepsFacts(before: Snapshot, event: NewRunEvent, after: Snapshot): void {
  if (INPUTS.includes(event.type) && before.matches({editing: {facts: 'ready'}})) {
    expect(after.matches({editing: {facts: 'ready'}})).toBe(true);
  }
}

/** An input the user changes while editing is the form's from then on. */
function expectInputApplied(before: Snapshot, event: NewRunEvent, after: Snapshot): void {
  if (!before.matches('editing') || !after.matches('editing')) return;
  const {form} = after.context;
  if (event.type === 'task') expect(form.task).toBe(event.task);
  if (event.type === 'budget') expect(form.budget).toEqual(event.budget);
  if (event.type === 'advanced') expect(form.advanced).toBe(event.advanced);
  if (event.type === 'project') expect(form.project).toEqual(event.project);
}

describe('newRunMachine', () => {
  test('model: no stale work, Start only on passing checks for the current inputs', () => {
    checkMachine(
      {
        machine: newRunMachine,
        input: {machine: 'local', recent: RECENT},
        event: (random, live, snapshot) =>
          random.pick([
            inputEvent(random, snapshot),
            inputEvent(random, snapshot),
            ...live.operations.map(operation => live.done(operation, output(operation, random))),
            ...live.operations.map(operation => live.error(operation, new Error('refused'))),
            ...live.delays.map(delay => live.fire(delay)),
          ]),
        invariants: snapshot => {
          const {form, harnesses} = snapshot.context;
          const problems: string[] = [];
          if (form.agent !== null && harnesses !== null) {
            if (!vettedModels(harnesses.list, form.agent.provider).includes(form.agent.model)) {
              problems.push(`model ${form.agent.model} is not vetted for ${form.agent.provider}`);
            }
          }
          if (form.project?.source === 'example' && runInputs(form) !== null) {
            problems.push('an example not yet created has run inputs');
          }
          return problems;
        },
        also: (before, event, after) => {
          expectStartOnlyWhenChecksPass(before, after);
          expectInputKeepsFacts(before, event as NewRunEvent, after);
          expectInputApplied(before, event as NewRunEvent, after);
          if (event.type === 'xstate.done.actor.preflight') {
            expect(after.context.checks?.for).toBe(inputsKey(after.context.form) ?? undefined);
          }
        },
      },
      60,
    );
  });

  test('prefills from the last run on the machine and picks its default agent', async () => {
    const actor = createActor(
      newRunMachine.provide({
        actors: {
          describe: fromPromise<Offered<MachineDescription>, {readonly host: HostKey}>(
            async () => ({
              kind: 'unsupported',
            }),
          ),
          examples: fromPromise<Offered<ExampleList>, {readonly host: HostKey}>(async () => ({
            kind: 'unsupported',
          })),
          harnesses: fromPromise<readonly HarnessOffer[], {readonly host: HostKey}>(
            async () => OFFERS,
          ),
          tasks: fromPromise<readonly string[], {readonly host: HostKey; readonly project: string}>(
            async () => ['spsc', 'mpmc'],
          ),
        },
      }),
      {input: {machine: 'local', recent: RECENT}},
    );
    actor.start();
    await new Promise<void>(resolve => {
      const done = actor.subscribe(snapshot => {
        if (snapshot.matches({editing: {tasks: 'ready', harnesses: 'ready'}})) {
          done.unsubscribe();
          resolve();
        }
      });
    });
    const {form} = actor.getSnapshot().context;
    expect(form.project).toEqual({source: 'recent', path: '/p/queue'});
    expect(form.task).toBe('mpmc');
    expect(form.agent).toMatchObject({provider: 'claude', model: 'claude-a'});
    actor.stop();
  });
});
