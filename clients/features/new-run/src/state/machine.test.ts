import {describe, expect, test} from 'bun:test';
import type {
  Check,
  ExampleList,
  HelperFeature,
  MachineDescription,
} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';
import type {HarnessOffer, Offered, PreflightResult, RunInputs} from '@vibesys/ipc';
import {checkMachine} from '@vibesys/machine/testing';
import type {RecentRun} from '@vibesys/runs';
import type {Seeded} from '@vibesys/store/testing';
import {createActor, fromPromise, SimulatedClock, type SnapshotFrom} from 'xstate';
import {
  CHECK_DEBOUNCE_MS,
  checksPass,
  formWithRoles,
  inputsKey,
  missingFeatures,
  type NewRunEvent,
  newRunMachine,
  offers,
  prefill,
  recordedAgent,
  roleLabel,
  roleSummary,
  rolesAfter,
  runInputs,
  splitWords,
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
    start: {
      harness: {provider: 'claude', model: 'claude-b'},
      options: {
        rounds: 4,
        time_limit_s: 7200,
        results: 'publish_github',
        roles: [{role: 'judge', model: 'claude-a', reasoning_effort: 'high'}],
      },
      args: ['--max-in-flight', '3', '--note', 'two words'],
    },
  },
];
const ROLE_IDS = ['multi-orchestrator', 'multi-profiler', 'multi-implementer', 'multi-judge'];
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
    case 'features':
      // A current helper, one before examples, one before typed options, one before both.
      return random.pick([
        ['run_options', 'preflight', 'examples', 'harnesses', 'run_roles'],
        ['run_options', 'preflight', 'examples', 'harnesses'],
        ['run_options', 'preflight', 'harnesses'],
        ['preflight', 'examples', 'harnesses'],
        ['harnesses'],
      ]);
    case 'tasks':
      return random.pick([['mpmc', 'spsc'], ['spsc'], []]);
    case 'preflight':
      return {
        kind: 'offered',
        value: {
          checks: checks(random),
          // From a helper without `run_roles`, from settings that resolved none, or the run's own.
          roles: random.pick([null, [], ROLE_IDS, ROLE_IDS.slice(1), ['plain-judge']]),
          backend: random.pick([null, 'cpu', 'cuda']),
        },
      };
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
      role: {role: random.pick(ROLE_IDS), model: null, effort: random.pick(['default', 'high'])},
    },
    {type: 'budget', budget: {rounds: random.pick([1, 2, 5]), timeLimitS: null, results: 'keep'}},
    {type: 'advanced', advanced: random.pick(['', '--max-in-flight 3', '--max-rounds 4', '"open'])},
    {type: 'again', run: random.pick(RECENT)},
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

/**
 * The helper is asked only what it agreed to: the checks need `preflight` and `run_options`, the
 * examples need `examples`, and Start needs every feature New run uses.
 */
function expectAsksOnlyWhatHelperTakes(before: Snapshot, after: Snapshot): void {
  const {helper, form} = after.context;
  const entered = (state: Parameters<Snapshot['matches']>[0]) =>
    !before.matches(state) && after.matches(state);
  if (entered({editing: {checks: 'checking'}})) {
    expect(offers(helper, form.machine, 'preflight')).toBe(true);
    expect(offers(helper, form.machine, 'run_options')).toBe(true);
  }
  if (entered({editing: {examples: 'loading'}})) {
    expect(offers(helper, form.machine, 'examples')).toBe(true);
  }
  if (entered({editing: {action: 'starting'}})) {
    expect(offers(helper, form.machine, 'run_options')).toBe(true);
    expect(missingFeatures(helper, form.machine)).toEqual([]);
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

/** The form never sets a role the run does not have, once the run has reported its roles. */
function unreportedRoles(snapshot: Snapshot): string[] {
  const {form, roles} = snapshot.context;
  if (form.agent === null || roles === null) return [];
  return form.agent.roles
    .filter(choice => !roles.includes(choice.role))
    .map(choice => `role ${choice.role} is not the run's`);
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
          problems.push(...unreportedRoles(snapshot));
          if (form.project?.source === 'example' && runInputs(form) !== null) {
            problems.push('an example not yet created has run inputs');
          }
          return problems;
        },
        also: (before, event, after) => {
          expectStartOnlyWhenChecksPass(before, after);
          expectInputKeepsFacts(before, event as NewRunEvent, after);
          expectInputApplied(before, event as NewRunEvent, after);
          expectAsksOnlyWhatHelperTakes(before, after);
          if (event.type === 'xstate.done.actor.preflight') {
            // The checks are for the inputs that were asked; the roles the run reports may then
            // change the form, and the checks are no longer for it (Start waits for new ones).
            expect(after.context.checks?.for).toBe(inputsKey(before.context.form) ?? undefined);
          }
        },
      },
      60,
    );
  });

  test('regression: running a run again keeps its model, budget, results and extra arguments', async () => {
    const again = RECENT[2] as RecentRun;
    const actor = createActor(
      newRunMachine.provide({
        actors: {
          describe: fromPromise<Offered<MachineDescription>, {readonly host: HostKey}>(
            async () => ({kind: 'unsupported'}),
          ),
          examples: fromPromise<Offered<ExampleList>, {readonly host: HostKey}>(async () => ({
            kind: 'unsupported',
          })),
          harnesses: fromPromise<readonly HarnessOffer[], {readonly host: HostKey}>(
            async () => OFFERS,
          ),
          features: fromPromise<readonly HelperFeature[], {readonly host: HostKey}>(async () => [
            'run_options',
            'preflight',
            'examples',
          ]),
          tasks: fromPromise<readonly string[], {readonly host: HostKey; readonly project: string}>(
            async () => ['mpmc', 'spsc'],
          ),
        },
      }),
      {input: {machine: 'local', recent: RECENT}},
    );
    actor.start();
    actor.send({type: 'again', run: again});
    await new Promise<void>(resolve => {
      const done = actor.subscribe(snapshot => {
        if (snapshot.matches({editing: {tasks: 'ready', harnesses: 'ready'}})) {
          done.unsubscribe();
          resolve();
        }
      });
    });
    const {form} = actor.getSnapshot().context;
    expect(form.machine).toBe('ssh:gpu');
    expect(form.project).toEqual({source: 'recent', path: '/q'});
    expect(form.task).toBe('spsc');
    expect(form.agent).toMatchObject({provider: 'claude', model: 'claude-b'});
    expect(form.agent?.roles.find(role => role.role === 'judge')).toEqual({
      role: 'judge',
      model: 'claude-a',
      effort: 'high',
    });
    expect(form.budget).toEqual({rounds: 4, timeLimitS: 7200, results: 'publish_github'});
    expect(splitWords(form.advanced)).toEqual(['--max-in-flight', '3', '--note', 'two words']);
    expect(runInputs(form)).toMatchObject({
      harness: {provider: 'claude', model: 'claude-b'},
      options: {rounds: 4, time_limit_s: 7200, results: 'publish_github'},
      args: ['--max-in-flight', '3', '--note', 'two words'],
    });
    actor.stop();
  });

  test('running a run again whose harness is no longer offered falls back to the default', async () => {
    const stale: RecentRun = {
      ...(RECENT[2] as RecentRun),
      start: {harness: {provider: 'claude', model: 'claude-gone'}, options: {}, args: []},
    };
    expect(recordedAgent(stale.start ?? null, OFFERS)).toMatchObject({
      provider: 'claude',
      model: 'claude-a',
    });
    expect(recordedAgent(null, OFFERS)).toMatchObject({provider: 'claude', model: 'claude-a'});
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
          features: fromPromise<readonly HelperFeature[], {readonly host: HostKey}>(async () => [
            'run_options',
            'preflight',
            'examples',
          ]),
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

describe('the run reports its roles', () => {
  /** An actor on a helper that reports `roles`, noting the inputs of each preflight in `asked`. */
  function actorOn(roles: readonly string[], asked: RunInputs[], clock: SimulatedClock) {
    return createActor(
      newRunMachine.provide({
        actors: {
          describe: fromPromise<Offered<MachineDescription>, {readonly host: HostKey}>(
            async () => ({kind: 'unsupported'}),
          ),
          examples: fromPromise<Offered<ExampleList>, {readonly host: HostKey}>(async () => ({
            kind: 'unsupported',
          })),
          harnesses: fromPromise<readonly HarnessOffer[], {readonly host: HostKey}>(
            async () => OFFERS,
          ),
          features: fromPromise<readonly HelperFeature[], {readonly host: HostKey}>(async () => [
            'run_options',
            'preflight',
            'examples',
            'run_roles',
          ]),
          tasks: fromPromise<readonly string[], {readonly host: HostKey; readonly project: string}>(
            async () => ['spsc'],
          ),
          preflight: fromPromise<
            Offered<PreflightResult>,
            {readonly host: HostKey; readonly inputs: RunInputs}
          >(async ({input}) => {
            asked.push(input.inputs);
            return {kind: 'offered', value: {checks: [], roles, backend: null}};
          }),
        },
      }),
      {input: {machine: 'local', recent: RECENT}, clock},
    );
  }

  function until(actor: ReturnType<typeof actorOn>, ready: (snapshot: Snapshot) => boolean) {
    return new Promise<void>(resolve => {
      if (ready(actor.getSnapshot())) {
        resolve();
        return;
      }
      const done = actor.subscribe(snapshot => {
        if (ready(snapshot)) {
          done.unsubscribe();
          resolve();
        }
      });
    });
  }

  const LOADED = {editing: {tasks: 'ready', harnesses: 'ready', helper: 'ready'}} as const;

  test('they replace the role settings a recorded run had that this run lacks, and are asked again', async () => {
    const asked: RunInputs[] = [];
    const clock = new SimulatedClock();
    const actor = actorOn(['multi-orchestrator', 'multi-implementer'], asked, clock);
    actor.start();
    actor.send({type: 'again', run: RECENT[2] as RecentRun});
    await until(actor, snapshot => snapshot.matches(LOADED));
    // The recorded run set `judge`, which the reported roles do not have.
    expect(actor.getSnapshot().context.form.agent?.roles.map(role => role.role)).toEqual(['judge']);
    clock.increment(CHECK_DEBOUNCE_MS);
    await until(actor, snapshot => snapshot.context.roles !== null);
    const {context} = actor.getSnapshot();
    expect(context.roles).toEqual(['multi-orchestrator', 'multi-implementer']);
    expect(context.form.agent?.roles).toEqual([
      {role: 'multi-orchestrator', model: null, effort: 'default'},
      {role: 'multi-implementer', model: null, effort: 'default'},
    ]);
    // The dropped setting changed the inputs: they are checked again, without it.
    clock.increment(CHECK_DEBOUNCE_MS);
    await until(actor, () => asked.length === 2);
    expect(asked[0]?.options.roles).toEqual([
      {role: 'judge', model: 'claude-a', reasoning_effort: 'high'},
    ]);
    expect(asked[1]?.options.roles).toEqual([]);
    actor.stop();
  });

  test('an answer that names no roles leaves them unknown', async () => {
    const asked: RunInputs[] = [];
    const clock = new SimulatedClock();
    const actor = actorOn([], asked, clock);
    actor.start();
    await until(actor, snapshot => snapshot.matches(LOADED));
    clock.increment(CHECK_DEBOUNCE_MS);
    await until(actor, () => asked.length === 1);
    await until(actor, snapshot => snapshot.matches({editing: {checks: 'ready'}}));
    expect(actor.getSnapshot().context.roles).toBeNull();
    actor.stop();
  });
});

describe('roles', () => {
  test('a role id reads as a label, and only the roles this copy knows say what they do', () => {
    expect(roleLabel('multi-implementer')).toBe('Multi implementer');
    expect(roleSummary('multi-implementer')).toBe('Writes candidates');
    expect(roleSummary('implementer')).toBe('Writes candidates');
    expect(roleSummary('multi-unheard-of')).toBeNull();
  });

  test('the form follows the roles it is given, keeping the settings of the roles it already had', () => {
    const agent = {
      provider: 'claude',
      model: 'claude-a',
      roles: [
        {role: 'a', model: 'claude-b', effort: 'high'},
        {role: 'gone', model: null, effort: 'low'},
      ],
    } as const;
    const form = {...prefill('local', []), agent};
    const followed = formWithRoles(form, ['b', 'a']);
    expect(followed.agent?.roles).toEqual([
      {role: 'b', model: null, effort: 'default'},
      {role: 'a', model: 'claude-b', effort: 'high'},
    ]);
    expect(formWithRoles(followed, ['b', 'a'])).toBe(followed);
    expect(formWithRoles(form, null)).toBe(form);
    expect(rolesAfter(['a'], [])).toEqual(['a']);
    expect(rolesAfter(['a'], ['b'])).toEqual(['b']);
  });
});
