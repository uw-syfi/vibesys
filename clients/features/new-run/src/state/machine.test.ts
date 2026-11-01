import {describe, expect, test} from 'bun:test';
import type {
  Check,
  ExampleList,
  HelperFeature,
  MachineDescription,
  TaskGoal,
} from '@vibesys/contracts/host-helper';
import {type HostKey, REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import type {
  ComputeSettings,
  DestinationFound,
  HarnessOffer,
  MeasureTarget,
  Offered,
  PreflightResult,
  RunInputs,
} from '@vibesys/ipc';
import {checkMachine, type Live} from '@vibesys/machine/testing';
import {exampleCatalogue} from '@vibesys/run-fixtures';
import type {RecentRun} from '@vibesys/runs';
import type {Seeded} from '@vibesys/store/testing';
import {createActor, fromPromise, SimulatedClock, type SnapshotFrom} from 'xstate';
import {type AnswerChange, chosenEntry, NO_ANSWERS, stepIds} from './answers.js';
import type {CatalogueEntry} from './catalogue.js';
import type {Draft} from './draft.js';
import {catalogueFor, examplesStatus} from './flow.js';
import {
  CHECK_DEBOUNCE_MS,
  type Compute,
  checksPass,
  destinationKey,
  EXAMPLES_BOUND_MS,
  type FixDone,
  formWithRoles,
  inputsKey,
  isReading,
  measureOptions,
  missingFeatures,
  type NewRunEvent,
  newRunMachine,
  offers,
  type ProjectFix,
  prefill,
  recordedAgent,
  roleSummary,
  rolesAfter,
  runInputs,
  sameTarget,
  splitWords,
  startUnconfirmed,
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
        roles: [{role: 'judge', model: null, reasoning_effort: 'high'}],
      },
      agents: {judge: {provider: 'claude', model: 'claude-a'}},
      measure_on: {kind: 'same_machine'},
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

/** A machine that has the CLIs of `harnessClis` installed and nothing else the tests look at. */
function machineWith(harnessClis: readonly string[]): MachineDescription {
  return {
    checkout: null,
    hardware: {cpu_cores: 8, gpus: [], accelerator: 'cpu'},
    docker: {reachable: true, version: '29.1'},
    harness_clis: [...harnessClis],
    runs: {live: 0, ended_today: 0},
  };
}

/** What each operation answers: plausible values, chosen at random. */
function output(operation: string, random: Seeded): unknown {
  switch (operation) {
    case 'describe':
      return random.pick([
        {kind: 'unsupported'},
        {
          kind: 'offered',
          value: machineWith(random.pick([[], ['claude'], ['codex'], ['mystery']])),
        },
      ]);
    case 'examples':
      return random.pick([{kind: 'unsupported'}, {kind: 'offered', value: exampleCatalogue}]);
    case 'harnesses':
      return OFFERS;
    case 'features':
      // A current helper, one before examples, one before typed options, one before both.
      return random.pick([
        [...REQUIRED_HELPER_FEATURES, 'harnesses', 'run_roles', 'project_goal'],
        [...REQUIRED_HELPER_FEATURES, 'harnesses', 'example_destination'],
        [...REQUIRED_HELPER_FEATURES, 'harnesses'],
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
    case 'goal':
      return random.pick([
        {kind: 'unsupported'},
        {
          kind: 'offered',
          value: {task: 'mpmc', objectives: [], goal: 'Go fast.', goal_truncated: false},
        },
      ]);
    case 'destination':
      return random.pick<Offered<DestinationFound>>([
        {kind: 'unsupported'},
        {kind: 'offered', value: {state: 'absent', project: null}},
        {kind: 'offered', value: {state: 'empty', project: null}},
        {kind: 'offered', value: {state: 'occupied', project: null}},
        {kind: 'offered', value: {state: 'example_project', project: '/made/kv'}},
      ]);
    case 'fix':
      return {
        fix: random.pick<ProjectFix>([
          {kind: 'copy_out', source: '/p/queue', destination: '/own/a'},
          {kind: 'create_from_example', example: 'database/kv-store', destination: '~/kv'},
        ]),
        // A destination that turned out to be taken is a fix done without a project.
        created: random.pick([
          {project: random.pick(['/own/a', '/own/b']), tasks: ['mpmc']},
          {project: '/own/a', tasks: ['mpmc']},
          null,
        ]),
      };
    default:
      return {runId: 'run-new', instanceId: '0123456789ab'};
  }
}

/** An answer a person could give at the step shown now (or any step, to also try going back). */
function answerChange(random: Seeded): AnswerChange {
  return random.pick<AnswerChange>([
    {kind: 'from', from: 'example'},
    {kind: 'from', from: 'project'},
    {kind: 'domain', domain: random.pick(['llm-serving', 'data-structures', 'databases'] as const)},
    {kind: 'model', model: random.pick(['Llama 3 8B Instruct', 'Qwen3.5 9B'])},
    {kind: 'hardware', hardware: random.pick(['nvidia|NVIDIA H100|2', 'amd|AMD MI210|1'])},
    {kind: 'structure', example: 'data-structures/stack'},
    {kind: 'workload', task: random.pick(['mpmc', 'spsc'])},
    {
      kind: 'example',
      example: random.pick(['database/kv-store', 'microservices/hotel-correctness']),
    },
    {kind: 'variant', task: random.pick(['from-scratch', 'vllm'])},
    {kind: 'project', project: {source: 'folder', path: random.pick(['/p/queue', '/p/kv'])}},
    {kind: 'task', task: random.pick(['mpmc', 'spsc'])},
    {kind: 'goal'},
  ]);
}

/** The agent a person picks: a harness, and one of the models the host vets for it. */
function agentEvent(random: Seeded): NewRunEvent {
  const provider = random.pick(['claude', 'codex']);
  return {type: 'agent', provider, model: random.pick(vettedModels(OFFERS, provider))};
}

function inputEvent(random: Seeded, snapshot: SnapshotFrom<typeof newRunMachine>): NewRunEvent {
  return random.pick<NewRunEvent>([
    {type: 'machine', machine: random.pick(['local', 'ssh:gpu'])},
    {type: 'answer', change: answerChange(random), advance: random.bool()},
    {type: 'answer', change: answerChange(random), advance: random.bool()},
    {type: 'go', step: random.pick(stepIds(snapshot.context.answers, []))},
    {type: 'next'},
    {type: 'back'},
    {type: 'reset'},
    agentEvent(random),
    {
      type: 'role',
      role: {role: random.pick(ROLE_IDS), agent: null, effort: random.pick(['default', 'high'])},
    },
    {type: 'budget', budget: {rounds: random.pick([1, 2, 5]), timeLimitS: null, results: 'keep'}},
    {
      type: 'measure',
      target: random.pick<MeasureTarget>([
        {kind: 'same_machine'},
        {kind: 'modal'},
        {kind: 'slurm', cluster: random.pick(['lab-h100', 'cluster-m'])},
      ]),
    },
    {
      type: 'compute',
      machine: random.pick(['local', 'ssh:gpu']),
      compute: random.pick<Compute>([
        null,
        {kind: 'unsupported'},
        {kind: 'offered', value: SETTINGS_WITH_MODAL},
        {kind: 'offered', value: SETTINGS_WITH_LAB},
      ]),
    },
    {type: 'advanced', advanced: random.pick(['', '--max-in-flight 3', '--max-rounds 4', '"open'])},
    {type: 'destination', destination: random.pick(['~/a', '~/b'])},
    {type: 'again', run: random.pick(RECENT)},
    {type: 'recheck'},
    {type: 'fix', fix: {kind: 'copy_out', source: '/p/queue', destination: '/own/a'}},
    {
      type: 'fix',
      fix: {kind: 'create_from_example', example: 'database/kv-store', destination: '~/kv'},
    },
    {
      type: 'use_created',
      example: random.pick(['database/kv-store', 'data-structures/stack']),
      destination: '~/kv',
      path: '/made/kv',
    },
    {type: 'start'},
    {type: 'looked'},
  ]);
}

type Snapshot = SnapshotFrom<typeof newRunMachine>;

/** Start is entered only from inputs whose checks are current and pass. */
function expectStartOnlyWhenChecksPass(before: Snapshot, after: Snapshot): void {
  const starting = (snapshot: Snapshot) => snapshot.matches({editing: {action: 'starting'}});
  if (!starting(before) && starting(after)) {
    expect(checksPass(before.context.form, before.context.checks)).toBe(true);
    // A start that may have happened is not repeated until the person has looked.
    expect(startUnconfirmed(before.context.problem)).toBe(false);
  }
}

function settings(clusters: readonly string[], modal: boolean): ComputeSettings {
  return {
    config_path: '/home/me/.config/vibesys/config.toml',
    clusters: clusters.map(name => ({
      cluster: {name, reach: {kind: 'login_node'}, workspace: '/scratch/me'},
      source: 'user',
    })),
    modal: {token_present: modal},
  } as unknown as ComputeSettings;
}

const SETTINGS_WITH_MODAL = settings([], true);
const SETTINGS_WITH_LAB = settings(['lab-h100'], false);

const INPUTS = ['answer', 'agent', 'role', 'budget', 'measure', 'advanced', 'destination'];

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
  if (entered({editing: {goal: 'loading'}})) {
    expect(offers(helper, form.machine, 'project_goal')).toBe(true);
    expect(after.context.answers.from).toBe('project');
    expect(after.context.answers.task).not.toBeNull();
  }
  if (entered({editing: {destination: 'loading'}})) {
    expect(offers(helper, form.machine, 'example_destination')).toBe(true);
    expect(form.project?.source).toBe('example');
  }
  if (entered({editing: {examples: 'loading'}})) {
    expect(offers(helper, form.machine, 'examples')).toBe(true);
  }
  if (entered({editing: {action: 'starting'}})) {
    expect(offers(helper, form.machine, 'run_options')).toBe(true);
    expect(missingFeatures(helper, form.machine)).toEqual([]);
  }
}

/** Looking among the runs after a start that may have happened ends that problem, Start opens. */
function expectLookingOpensStart(before: Snapshot, event: NewRunEvent, after: Snapshot): void {
  if (event.type === 'looked' && startUnconfirmed(before.context.problem)) {
    expect(after.context.problem).toBeNull();
  }
}

/** An input the user changes while editing is the form's from then on. */
function expectInputApplied(before: Snapshot, event: NewRunEvent, after: Snapshot): void {
  if (!before.matches('editing') || !after.matches('editing')) return;
  const {form} = after.context;
  if (event.type === 'budget') expect(form.budget).toEqual(event.budget);
  if (event.type === 'advanced') expect(form.advanced).toBe(event.advanced);
  if (event.type === 'agent') {
    expect(form.agent).toMatchObject({provider: event.provider, model: event.model});
  }
}

/** The operations that answer a press of the user; every other one is a read the screen started. */
const ANSWERS_TO_INPUTS: ReadonlySet<string> = new Set(['fix', 'start']);

/**
 * `isReading` says whether the screen still waits on a read it started: it holds while one is live
 * (or one is about to start: the examples' bound, the checks' debounce), and holds no longer once
 * none is. So the screen is never ready
 * with a read outstanding, a read added without its tag is caught here, and it is never loading
 * with nothing left to end it.
 */
function readingProblems(snapshot: Snapshot, live: Live): string[] {
  const reads = live.operations.filter(operation => !ANSWERS_TO_INPUTS.has(operation));
  if (reads.length > 0 && !isReading(snapshot)) {
    return [`${reads.join(', ')} is live but the screen is not reading`];
  }
  // Waiting on a bound or a debounce is waiting on a read that has not started yet.
  const waiting = live.delays.some(delay => /examplesBound|debounce/.test(delay));
  if (isReading(snapshot) && reads.length === 0 && !waiting) {
    return ['the screen is reading with nothing live to end it'];
  }
  return [];
}

/** The run is never measured on a target the machine's Settings data says it does not offer. */
function unofferedMeasure(snapshot: Snapshot): string[] {
  const {compute, form} = snapshot.context;
  const offered =
    compute === null || measureOptions(compute).some(option => sameTarget(option, form.measure));
  return offered ? [] : ['the run is measured on a target its machine does not offer'];
}

/** The form never sets a role the run does not have, once the run has reported its roles. */
function unreportedRoles(snapshot: Snapshot): string[] {
  const {form, roles} = snapshot.context;
  if (form.agent === null || roles === null) return [];
  return form.agent.roles
    .filter(choice => !roles.includes(choice.role))
    .map(choice => `role ${choice.role} is not the run's`);
}

/**
 * The guided form agrees with itself: where the catalogue is known (or the project is the user's
 * own), the step shown is one the answers ask, and the project and task of the run are the ones
 * the answers name.
 */
function flowProblems(snapshot: Snapshot): string[] {
  const {answers, at, form, examples} = snapshot.context;
  const entries = catalogueFor(examples, form.machine);
  if (entries === null && answers.from === 'example') return [];
  const known = entries ?? [];
  const problems = stepIds(answers, known).includes(at) ? [] : [`the step ${at} is not asked`];
  if (answers.from === 'example') return [...problems, ...exampleProblems(snapshot, known)];
  return [...problems, ...ownProblems(snapshot)];
}

/** The run is of the user's own project, or of nothing yet: its project and task are the answers'. */
function ownProblems(snapshot: Snapshot): string[] {
  const {answers, form} = snapshot.context;
  if (answers.from !== 'project') {
    return form.project === null && form.task === null
      ? []
      : ['a run is named before anything is answered'];
  }
  return [
    ...(JSON.stringify(form.project) === JSON.stringify(answers.project)
      ? []
      : ['the project is not the one answered']),
    ...(form.task === answers.task ? [] : ['the task is not the one answered']),
  ];
}

/** The run is of an example: its project and task are the ones its answers name. */
function exampleProblems(snapshot: Snapshot, entries: readonly CatalogueEntry[]): string[] {
  const {answers, form} = snapshot.context;
  const entry = chosenEntry(answers, entries);
  const made = form.project?.source === 'example' || form.project?.source === 'created';
  return [
    ...(form.task === (entry?.task ?? null) ? [] : ["the task is not the example's"]),
    ...((form.project === null) === (entry === null) ? [] : ['the example project disagrees']),
    ...(entry === null || made ? [] : ['an example is not an example project']),
  ];
}

describe('newRunMachine', () => {
  test('model: no stale work, Start only on passing checks for the current inputs', () => {
    checkMachine(
      {
        machine: newRunMachine,
        input: {machine: 'local', recent: RECENT, moreOpen: {}},
        event: (random, live, snapshot) =>
          random.pick([
            inputEvent(random, snapshot),
            inputEvent(random, snapshot),
            ...live.operations.map(operation => live.done(operation, output(operation, random))),
            ...live.operations.map(operation => live.error(operation, new Error('refused'))),
            ...live.delays.map(delay => live.fire(delay)),
          ]),
        invariants: (snapshot, live) => {
          const {form, harnesses} = snapshot.context;
          const problems: string[] = readingProblems(snapshot, live);
          if (form.agent !== null && harnesses !== null) {
            if (!vettedModels(harnesses.list, form.agent.provider).includes(form.agent.model)) {
              problems.push(`model ${form.agent.model} is not vetted for ${form.agent.provider}`);
            }
          }
          problems.push(...unreportedRoles(snapshot));
          problems.push(...unofferedMeasure(snapshot));
          if (form.project?.source === 'example' && runInputs(form) !== null) {
            problems.push('an example not yet created has run inputs');
          }
          problems.push(...flowProblems(snapshot));
          return problems;
        },
        also: (before, event, after) => {
          expectStartOnlyWhenChecksPass(before, after);
          expectLookingOpensStart(before, event as NewRunEvent, after);
          expectInputKeepsFacts(before, event as NewRunEvent, after);
          expectInputApplied(before, event as NewRunEvent, after);
          expectAsksOnlyWhatHelperTakes(before, after);
          if (event.type === 'xstate.done.actor.goal' && after.context.goal !== null) {
            // A goal is kept under the machine, project and task it was asked of.
            expect(JSON.parse(after.context.goal.for)[0]).toBe(before.context.form.machine);
          }
          if (event.type === 'xstate.done.actor.destination') {
            // What the destination held is kept for the machine, example and place that were asked.
            expect(after.context.destination?.for).toBe(
              destinationKey(before.context.form) ?? undefined,
            );
          }
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
            ...REQUIRED_HELPER_FEATURES,
          ]),
          tasks: fromPromise<readonly string[], {readonly host: HostKey; readonly project: string}>(
            async () => ['mpmc', 'spsc'],
          ),
        },
      }),
      {input: {machine: 'local', recent: RECENT, moreOpen: {}}},
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
      agent: {provider: 'claude', model: 'claude-a'},
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
      start: {
        harness: {provider: 'claude', model: 'claude-gone'},
        options: {},
        agents: {},
        measure_on: {kind: 'same_machine'},
        args: [],
      },
    };
    expect(recordedAgent(stale.start ?? null, OFFERS)).toMatchObject({
      provider: 'claude',
      model: 'claude-a',
    });
    expect(recordedAgent(null, OFFERS)).toMatchObject({provider: 'claude', model: 'claude-a'});
  });

  test("a new form knows no project; it carries the last run's agent and budget on its machine", async () => {
    const actor = liveActor({machine: 'ssh:gpu'});
    actor.start();
    await until(actor, snapshot => snapshot.matches({editing: {harnesses: 'ready'}}));
    const {form, answers, at} = actor.getSnapshot().context;
    expect(form.project).toBeNull();
    expect(form.task).toBeNull();
    expect(answers.from).toBeNull();
    expect(at).toBe('start');
    expect(form.agent).toMatchObject({provider: 'claude', model: 'claude-b'});
    expect(form.budget.rounds).toBe(4);
    actor.stop();
  });

  test("the tasks of the user's project are read; a project with one has it answered", async () => {
    const actor = liveActor({tasks: ['only']});
    actor.start();
    actor.send({type: 'answer', change: {kind: 'from', from: 'project'}, advance: true});
    actor.send({
      type: 'answer',
      change: {kind: 'project', project: {source: 'folder', path: '/p/solo'}},
      advance: true,
    });
    await until(actor, snapshot => snapshot.context.answers.task === 'only');
    const {answers, form, at} = actor.getSnapshot().context;
    expect(form.task).toBe('only');
    expect(answers.goalSeen).toBe(false);
    expect(at).toBe('task');
    actor.stop();
  });

  test("the goal of the user's own task is read once the helper can, and again for another task", async () => {
    const asked: string[] = [];
    const actor = liveActor({
      tasks: ['one', 'two'],
      features: [...REQUIRED_HELPER_FEATURES, 'project_goal'],
      goalsAsked: asked,
    });
    actor.start();
    await until(actor, snapshot => snapshot.matches({editing: {helper: 'ready'}}));
    const send = (change: AnswerChange) => actor.send({type: 'answer', change, advance: true});
    send({kind: 'from', from: 'project'});
    send({kind: 'project', project: {source: 'folder', path: '/p/queue'}});
    // No task yet: nothing to read.
    await until(actor, snapshot => snapshot.matches({editing: {tasks: 'ready'}}));
    expect(asked).toEqual([]);
    send({kind: 'task', task: 'one'});
    await until(actor, snapshot => snapshot.context.goal !== null);
    expect(asked).toEqual(['/p/queue one']);
    expect(actor.getSnapshot().context.goal).toEqual({
      for: JSON.stringify(['local', '/p/queue', 'one']),
      value: {
        kind: 'offered',
        value: {task: 'one', objectives: [], goal: 'one', goal_truncated: false},
      },
    });
    // Answering the goal as read asks nothing again; another task asks for its own.
    send({kind: 'goal'});
    send({kind: 'task', task: 'two'});
    await until(actor, snapshot => snapshot.context.goal?.for.includes('two') === true);
    expect(asked).toEqual(['/p/queue one', '/p/queue two']);
    actor.stop();
  });

  test('a helper without project_goal is never asked for a goal', async () => {
    const asked: string[] = [];
    const actor = liveActor({tasks: ['only'], goalsAsked: asked});
    actor.start();
    await until(actor, snapshot => snapshot.matches({editing: {helper: 'ready'}}));
    actor.send({type: 'answer', change: {kind: 'from', from: 'project'}, advance: true});
    actor.send({
      type: 'answer',
      change: {kind: 'project', project: {source: 'folder', path: '/p/solo'}},
      advance: true,
    });
    await until(actor, snapshot => snapshot.context.answers.task === 'only');
    await until(actor, snapshot => snapshot.matches({editing: {tasks: 'ready'}}));
    expect(asked).toEqual([]);
    expect(actor.getSnapshot().context.goal).toBeNull();
    actor.stop();
  });

  test('a task the project does not have is not kept, and the goal must be read again', async () => {
    const actor = liveActor({tasks: ['one', 'two']});
    actor.start();
    actor.send({type: 'again', run: RECENT[0] as RecentRun});
    await until(actor, snapshot => snapshot.matches({editing: {tasks: 'ready'}}));
    // The run's task, `mpmc`, is not among the project's.
    const {answers, at, form} = actor.getSnapshot().context;
    expect(answers.task).toBeNull();
    expect(form.task).toBeNull();
    expect(at).toBe('task');
    actor.stop();
  });

  test('an example answered through its questions is an example project until it is made', async () => {
    const actor = liveActor({examples: {kind: 'offered', value: exampleCatalogue}});
    actor.start();
    await until(actor, snapshot => snapshot.matches({editing: {examples: 'ready'}}));
    for (const change of [
      {kind: 'from', from: 'example'},
      {kind: 'domain', domain: 'data-structures'},
      {kind: 'structure', example: 'data-structures/stack'},
      {kind: 'workload', task: 'spsc'},
    ] as const) {
      actor.send({type: 'answer', change, advance: true});
    }
    let {form, at} = actor.getSnapshot().context;
    expect(at).toBe('goal');
    expect(form.task).toBe('spsc');
    expect(form.project).toEqual({
      source: 'example',
      example: 'data-structures/stack',
      destination: '~/vibesys-projects/stack',
    });
    actor.send({type: 'destination', destination: '~/mine'});
    actor.send({
      type: 'fix',
      fix: {kind: 'create_from_example', example: 'data-structures/stack', destination: '~/mine'},
    });
    await until(actor, snapshot => snapshot.context.form.project?.source === 'created');
    ({form} = actor.getSnapshot().context);
    expect(form.project).toEqual({
      source: 'created',
      example: 'data-structures/stack',
      destination: '~/mine',
      path: '/made/stack',
    });
    // Another task of the same example is in the project that was made, which is kept...
    actor.send({type: 'go', step: 'workload'});
    actor.send({type: 'answer', change: {kind: 'workload', task: 'mpmc'}});
    expect(actor.getSnapshot().context.form.project?.source).toBe('created');
    expect(actor.getSnapshot().context.form.task).toBe('mpmc');
    // ...and a different example is not in it.
    actor.send({type: 'go', step: 'domain'});
    actor.send({type: 'answer', change: {kind: 'domain', domain: 'databases'}});
    expect(actor.getSnapshot().context.form.project).toBeNull();
    actor.stop();
  });

  test("moving to another machine drops the user's project but keeps the example chosen", async () => {
    const actor = liveActor({examples: {kind: 'offered', value: exampleCatalogue}});
    actor.start();
    await until(actor, snapshot => snapshot.matches({editing: {examples: 'ready'}}));
    for (const change of [
      {kind: 'from', from: 'example'},
      {kind: 'domain', domain: 'databases'},
      {kind: 'example', example: 'database/kv-store'},
    ] as const) {
      actor.send({type: 'answer', change, advance: true});
    }
    actor.send({type: 'machine', machine: 'ssh:gpu'});
    await until(actor, snapshot => snapshot.matches({editing: {examples: 'ready'}}));
    expect(actor.getSnapshot().context.answers.example).toBe('database/kv-store');
    expect(actor.getSnapshot().context.form.project?.source).toBe('example');
    actor.send({type: 'reset'});
    actor.send({type: 'answer', change: {kind: 'from', from: 'project'}, advance: true});
    actor.send({
      type: 'answer',
      change: {kind: 'project', project: {source: 'recent', path: '/p/queue'}},
    });
    actor.send({type: 'machine', machine: 'local'});
    const {answers, form} = actor.getSnapshot().context;
    expect(answers.from).toBe('project');
    expect(answers.project).toBeNull();
    expect(form.project).toBeNull();
    actor.stop();
  });

  test('a draft restores the answers and the step, and the choices of the run', async () => {
    const draft = {
      machine: 'ssh:gpu',
      answers: {
        ...NO_ANSWERS,
        from: 'project',
        project: {source: 'recent', path: '/q'},
        task: 'spsc',
        goalSeen: true,
      },
      at: 'where',
      agent: null,
      budget: {rounds: 3, timeLimitS: null, results: 'keep'},
      measure: {kind: 'same_machine'},
      advanced: '',
      destination: null,
    } as const;
    const actor = liveActor({machine: 'ssh:gpu', draft});
    actor.start();
    const {context} = actor.getSnapshot();
    expect(context.at).toBe('where');
    expect(context.answers.task).toBe('spsc');
    expect(context.form).toMatchObject({
      machine: 'ssh:gpu',
      project: {source: 'recent', path: '/q'},
      task: 'spsc',
      budget: {rounds: 3},
    });
    await until(actor, snapshot => snapshot.matches({editing: {harnesses: 'ready'}}));
    // The agent is chosen once the machine's harnesses are known.
    expect(actor.getSnapshot().context.form.agent).not.toBeNull();
    actor.send({type: 'next'});
    expect(actor.getSnapshot().context.at).toBe('agent');
    actor.stop();
  });
});

/** An actor whose helper answers with what `options` says, and a way to wait for a state. */
function liveActor(options: {
  readonly machine?: HostKey;
  readonly examples?: Offered<ExampleList>;
  readonly tasks?: readonly string[];
  readonly draft?: Draft;
  /** What the helper agreed to; run options, checks and examples when not given. */
  readonly features?: readonly HelperFeature[];
  /** Every goal read, as `project task`; the helper answers each with the task's name as its goal. */
  readonly goalsAsked?: string[];
}) {
  return createActor(
    newRunMachine.provide({
      actors: {
        describe: fromPromise<Offered<MachineDescription>, {readonly host: HostKey}>(async () => ({
          kind: 'unsupported',
        })),
        examples: fromPromise<Offered<ExampleList>, {readonly host: HostKey}>(
          async () => options.examples ?? {kind: 'unsupported'},
        ),
        harnesses: fromPromise<readonly HarnessOffer[], {readonly host: HostKey}>(
          async () => OFFERS,
        ),
        features: fromPromise<readonly HelperFeature[], {readonly host: HostKey}>(
          async () => options.features ?? REQUIRED_HELPER_FEATURES,
        ),
        tasks: fromPromise<readonly string[], {readonly host: HostKey; readonly project: string}>(
          async () => options.tasks ?? ['mpmc', 'spsc'],
        ),
        goal: fromPromise<
          Offered<TaskGoal>,
          {readonly host: HostKey; readonly project: string; readonly task: string}
        >(async ({input}) => {
          options.goalsAsked?.push(`${input.project} ${input.task}`);
          return {
            kind: 'offered',
            value: {task: input.task, objectives: [], goal: input.task, goal_truncated: false},
          };
        }),
        fix: fromPromise<FixDone, {readonly host: HostKey; readonly fix: ProjectFix}>(
          async ({input}) => ({
            fix: input.fix,
            created: {
              project: input.fix.kind === 'copy_out' ? input.fix.destination : '/made/stack',
              tasks: ['spsc'],
            },
          }),
        ),
      },
    }),
    {
      input: {
        machine: options.machine ?? 'local',
        recent: RECENT,
        moreOpen: {},
        ...(options.draft === undefined ? {} : {draft: options.draft}),
      },
    },
  );
}

function until(
  actor: ReturnType<typeof liveActor>,
  ready: (snapshot: SnapshotFrom<typeof newRunMachine>) => boolean,
): Promise<void> {
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
            ...REQUIRED_HELPER_FEATURES,
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
      {input: {machine: 'local', recent: RECENT, moreOpen: {}}, clock},
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
      {role: 'multi-orchestrator', agent: null, effort: 'default'},
      {role: 'multi-implementer', agent: null, effort: 'default'},
    ]);
    // The dropped setting changed the inputs: they are checked again, without it.
    clock.increment(CHECK_DEBOUNCE_MS);
    await until(actor, () => asked.length === 2);
    expect(asked[0]?.options.roles).toEqual([
      {role: 'judge', model: null, reasoning_effort: 'high'},
    ]);
    expect(asked[0]?.agents).toEqual({judge: {provider: 'claude', model: 'claude-a'}});
    expect(asked[1]?.options.roles).toEqual([]);
    expect(asked[1]?.agents).toEqual({});
    actor.stop();
  });

  test('an answer that names no roles leaves them unknown', async () => {
    const asked: RunInputs[] = [];
    const clock = new SimulatedClock();
    const actor = actorOn([], asked, clock);
    actor.start();
    actor.send({type: 'again', run: RECENT[0] as RecentRun});
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
    expect(roleSummary('multi-implementer')).toBe('Writes candidates');
    expect(roleSummary('implementer')).toBe('Writes candidates');
    expect(roleSummary('multi-unheard-of')).toBeNull();
  });

  test('the form follows the roles it is given, keeping the settings of the roles it already had', () => {
    const agent = {
      provider: 'claude',
      model: 'claude-a',
      roles: [
        {role: 'a', agent: {provider: 'claude', model: 'claude-b'}, effort: 'high'},
        {role: 'gone', agent: null, effort: 'low'},
      ],
    } as const;
    const form = {...prefill('local', []), agent};
    const followed = formWithRoles(form, ['b', 'a']);
    expect(followed.agent?.roles).toEqual([
      {role: 'b', agent: null, effort: 'default'},
      {role: 'a', agent: {provider: 'claude', model: 'claude-b'}, effort: 'high'},
    ]);
    expect(formWithRoles(followed, ['b', 'a'])).toBe(followed);
    expect(formWithRoles(form, null)).toBe(form);
    expect(rolesAfter(['a'], [])).toEqual(['a']);
    expect(rolesAfter(['a'], ['b'])).toEqual(['b']);
  });
});

describe('a machine whose examples cannot be listed', () => {
  type Listing = Offered<ExampleList> | 'fails' | 'hangs';
  const never = (): Promise<never> => new Promise(() => {});

  /** An actor whose machines list as `listings` say (`local` and `ssh:gpu`), on a simulated clock. */
  function actorWith(
    listings: Readonly<Record<string, Listing>>,
    features: 'answers' | 'fails' | 'hangs',
    clock: SimulatedClock,
  ) {
    return createActor(
      newRunMachine.provide({
        actors: {
          describe: fromPromise<Offered<MachineDescription>, {readonly host: HostKey}>(
            async () => ({kind: 'unsupported'}),
          ),
          examples: fromPromise<Offered<ExampleList>, {readonly host: HostKey}>(async ({input}) => {
            const listing = listings[input.host] ?? 'fails';
            if (listing === 'fails') throw new Error('helper failed');
            if (listing === 'hangs') return never();
            return listing;
          }),
          harnesses: fromPromise<readonly HarnessOffer[], {readonly host: HostKey}>(
            async () => OFFERS,
          ),
          features: fromPromise<readonly HelperFeature[], {readonly host: HostKey}>(
            async ({input}) => {
              if (features === 'fails' && input.host === 'local') throw new Error('no helper');
              if (features === 'hangs' && input.host === 'local') return never();
              return REQUIRED_HELPER_FEATURES;
            },
          ),
        },
      }),
      {input: {machine: 'local', recent: RECENT, moreOpen: {}}, clock},
    );
  }

  const status = (actor: ReturnType<typeof actorWith>, machine: HostKey) =>
    examplesStatus(actor.getSnapshot().context.examples, machine);

  /** Let every promise already resolved deliver its answer (no real time passes). */
  const settle = async () => {
    for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
  };

  test.each([
    ['the listing fails', {local: 'fails'}, 'answers'],
    ['the helper cannot be reached', {local: 'fails'}, 'fails'],
  ] as const)('%s: failed is said at once, and another machine can be chosen', async (_name, listings, features) => {
    const clock = new SimulatedClock();
    const actor = actorWith(
      {...listings, 'ssh:gpu': {kind: 'offered', value: exampleCatalogue}},
      features,
      clock,
    );
    actor.start();
    await settle();
    expect(status(actor, 'local')).toBe('failed');
    actor.send({type: 'machine', machine: 'ssh:gpu'});
    await settle();
    expect(status(actor, 'ssh:gpu')).toBe('listed');
    actor.stop();
  });

  test.each([
    ['listing', {local: 'hangs'}, 'answers'],
    ['helper', {local: 'hangs'}, 'hangs'],
  ] as const)('a hanging %s is failed once the bound passes, not before', async (_name, listings, features) => {
    const clock = new SimulatedClock();
    const actor = actorWith(listings, features, clock);
    actor.start();
    await settle();
    expect(status(actor, 'local')).toBe('listing');
    clock.increment(EXAMPLES_BOUND_MS - 1);
    expect(status(actor, 'local')).toBe('listing');
    clock.increment(1);
    expect(status(actor, 'local')).toBe('failed');
    actor.stop();
  });

  test('choosing the same machine again lists it again', async () => {
    const clock = new SimulatedClock();
    const actor = actorWith({local: 'fails'}, 'answers', clock);
    actor.start();
    await settle();
    expect(status(actor, 'local')).toBe('failed');
    actor.send({type: 'machine', machine: 'local'});
    expect(status(actor, 'local')).toBe('listing');
    await settle();
    expect(status(actor, 'local')).toBe('failed');
    actor.stop();
  });
});
