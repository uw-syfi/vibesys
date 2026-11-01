/**
 * The New run screen's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"); the guided form is data in its context (`answers`, the step `at`, and the run's
 * `form`, which the answers name: `flow.ts`). While editing, parallel regions load what the
 * machine offers (its facts, vetted harnesses, examples), the project's tasks, and the "Ready to
 * start" checks for the current inputs, debounced; one more region runs a fix or the start.
 * Changing an input re-enters the regions that depend on it, which stops their work in flight, and
 * a reply is applied only for the inputs it was asked for, so a slow reply never overwrites newer
 * state. Start is enabled only when the checks are for the current inputs and none fails.
 */
import type {
  CreatedProject,
  ExampleList,
  HelperFeature,
  MachineDescription,
  TaskGoal,
} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';
import type {
  DestinationFound,
  HarnessOffer,
  MeasureTarget,
  Offered,
  PreflightResult,
  RunInputs,
  StartedRun,
} from '@vibesys/ipc';
import {unprovided} from '@vibesys/machine';
import type {RecentRun, RunStart} from '@vibesys/runs';
import {and, assign, raise, type SnapshotFrom, setup} from 'xstate';
import {installedFirst, roleProviders} from './agents.js';
import type {AnswerChange, Answers, StepId} from './answers.js';
import type {CatalogueEntry} from './catalogue.js';
import {type Draft, flowOfDraft} from './draft.js';
import {
  againAnswers,
  catalogueFor,
  type ExamplesFor,
  type Flow,
  flowAnswer,
  flowBack,
  flowGo,
  flowNext,
  flowSettled,
  startFlow,
} from './flow.js';
import {
  AGENT_ENVIRONMENT,
  type AgentChoice,
  againForm,
  type Budget,
  type ChecksFor,
  checksPass,
  type DestinationFor,
  defaultAgent,
  defaultRole,
  destinationKey,
  type FactsFor,
  type Form,
  type GoalFor,
  goalKey,
  type HelperFor,
  inputsKey,
  installedHarnesses,
  lastRun,
  missingFeatures,
  offers,
  prefill,
  projectPath,
  type RoleChoice,
  recordedAgent,
  recordedHarnessOffered,
  runInputs,
} from './form.js';
import {type Compute, formWithMeasure} from './measure.js';
import {type Problem, problemOf, type Step, startUnconfirmed} from './problem.js';
import {formWithRoles, rolesAfter, rolesWithin} from './roles.js';

/** The fixes the screen performs itself; the rest are the shell's (Machines) or the user's. */
export type ProjectFix =
  | {readonly kind: 'copy_out'; readonly source: string; readonly destination: string}
  | {readonly kind: 'create_from_example'; readonly example: string; readonly destination: string};

/**
 * A fix done: what was asked, and the project the helper made of it, or null when the example's
 * destination turned out to hold something (it is taken, which the screen says without an error).
 */
export interface FixDone {
  readonly fix: ProjectFix;
  readonly created: CreatedProject | null;
}

export interface NewRunContext {
  readonly form: Form;
  /** What the guided form has been told; `form.project` and `form.task` follow it. */
  readonly answers: Answers;
  /** The step shown. */
  readonly at: StepId;
  readonly recent: readonly RecentRun[];
  readonly facts: FactsFor | null;
  /** What the machine's helper agreed to: New run asks only what it takes. */
  readonly helper: HelperFor | null;
  /**
   * The harness offers of a machine: as the host `listed` them, and as the form offers them
   * (`list`: the ones installed on the machine first, once it has said which they are).
   */
  readonly harnesses: {
    readonly for: HostKey;
    readonly listed: readonly HarnessOffer[];
    readonly list: readonly HarnessOffer[];
  } | null;
  /**
   * Whether the form's agent is the machine's own pick, not the person's, a past run's or a
   * kept draft's: only that one follows what the machine reports installed.
   */
  readonly agentDefaulted: boolean;
  readonly examples: ExamplesFor | null;
  readonly tasks: {readonly for: string; readonly list: readonly string[]} | null;
  /** What the destination of the example chosen held, once read (feature `example_destination`). */
  readonly destination: DestinationFor | null;
  readonly checks: ChecksFor | null;
  /** What the machine has set up to measure on, from its Settings data (`compute` events). */
  readonly compute: Compute;
  /** The goal of the user's own task, once read (feature `project_goal`). */
  readonly goal: GoalFor | null;
  /**
   * The role ids the run's orchestration declares, from the latest preflight that reported any:
   * null before one did, or from a helper that does not report them (`offers(..., 'run_roles')`).
   */
  readonly roles: readonly string[] | null;
  /** What the run being run again was started with: its agent is chosen from it once known. */
  readonly rerun: RunStart | null;
  /** Which load, fix or start failed last and what was reported of it, until the next input. */
  readonly problem: Problem | null;
  readonly started: StartedRun | null;
  /**
   * Whether the user opened More options, by machine: absent until they chose. The screen keeps it
   * across starts and, through the store, across runs of the app.
   */
  readonly moreOpen: Readonly<Record<string, boolean>>;
}

export type NewRunEvent =
  | {readonly type: 'machine'; readonly machine: HostKey}
  | {readonly type: 'again'; readonly run: RecentRun}
  /** Answer the question of the step shown; `advance` moves on to the next step. */
  | {readonly type: 'answer'; readonly change: AnswerChange; readonly advance?: boolean}
  /** Show a step the answers reach. */
  | {readonly type: 'go'; readonly step: StepId}
  | {readonly type: 'next'}
  | {readonly type: 'back'}
  /** Forget the answers and start the form over on the same machine. */
  | {readonly type: 'reset'}
  | {readonly type: 'agent'; readonly provider: string; readonly model: string}
  | {readonly type: 'role'; readonly role: RoleChoice}
  | {readonly type: 'budget'; readonly budget: Budget}
  /** Where results are measured. */
  | {readonly type: 'measure'; readonly target: MeasureTarget}
  /** What `machine` has set up, as its Settings data says (null: not known); ignored for another machine. */
  | {readonly type: 'compute'; readonly machine: HostKey; readonly compute: Compute}
  | {readonly type: 'advanced'; readonly advanced: string}
  /** Where an example is copied to, before it is. */
  | {readonly type: 'destination'; readonly destination: string}
  | {readonly type: 'recent'; readonly recent: readonly RecentRun[]}
  | {readonly type: 'more_options'; readonly machine: HostKey; readonly open: boolean}
  | {readonly type: 'recheck'}
  /** The machine's helper could not be reached, so its examples are not coming. */
  | {readonly type: 'helper_failed'}
  /** Check again: read the project's tasks and check the inputs once more. */
  | {readonly type: 'refresh'}
  /** Try again the load or check that failed (`problem`). */
  | {readonly type: 'retry'}
  /** The person looked among their runs after a start that may have happened: Start is open again. */
  | {readonly type: 'looked'}
  | {readonly type: 'fix'; readonly fix: ProjectFix}
  /** Use the project that already is the example's, at `path`, without copying it again. */
  | {
      readonly type: 'use_created';
      readonly example: string;
      readonly destination: string;
      readonly path: string;
    }
  | {readonly type: 'start'};

export interface NewRunInput {
  readonly machine: HostKey;
  readonly recent: readonly RecentRun[];
  /** What the user chose for More options on each machine before this form. */
  readonly moreOpen: Readonly<Record<string, boolean>>;
  /** The unfinished form this window kept; none starts a new one. */
  readonly draft?: Draft | null;
}

/**
 * The tag of every state that waits on a read the screen started by itself: the helper's features,
 * the machine's facts, harnesses and examples, the project's tasks and goal, an example's
 * destination, and the checks of inputs that will be checked (the run's roles come from them). A
 * fix or start (`action`) answers a press of the user and is not among them. A new read gets the
 * tag on its state, or the model test of `isReading` fails on it.
 */
const READING = 'reading';

/** How long the inputs must stay still before they are checked, in milliseconds. */
export const CHECK_DEBOUNCE_MS = 400;

/**
 * How long a machine's examples may stay unlisted before the listing counts as failed. It only
 * guards a helper that never answers: a failure the helper reports shows at once, and the
 * connection gives up on its own sooner.
 */
export const EXAMPLES_BOUND_MS = 90_000;

function withAgent(form: Form, change: (agent: AgentChoice) => AgentChoice): Form {
  return form.agent === null ? form : {...form, agent: change(form.agent)};
}

/** The events that change one input of the run that the answers do not name. */
type InputEvent = Extract<
  NewRunEvent,
  {readonly type: 'agent' | 'role' | 'budget' | 'measure' | 'advanced' | 'destination'}
>;

/** The form after `event` changed one of its inputs. */
function withInput(context: NewRunContext, event: InputEvent): Form {
  const {form} = context;
  switch (event.type) {
    case 'agent': {
      // A role keeps its effort, and its own agent while the new run harness still allows it.
      const allowed = roleProviders(
        context.harnesses?.list ?? [],
        event.provider,
        AGENT_ENVIRONMENT,
      );
      const roles = form.agent?.roles ?? (context.roles ?? []).map(defaultRole);
      return {
        ...form,
        agent: {
          provider: event.provider,
          model: event.model,
          roles: rolesWithin(roles, allowed),
        },
      };
    }
    case 'role':
      return withAgent(form, agent => ({
        ...agent,
        roles: agent.roles.map(role => (role.role === event.role.role ? event.role : role)),
      }));
    case 'budget':
      return {...form, budget: event.budget};
    case 'measure':
      return formWithMeasure({...form, measure: event.target}, context.compute);
    case 'advanced':
      return {...form, advanced: event.advanced};
    case 'destination':
      return form.project?.source === 'example'
        ? {...form, project: {...form.project, destination: event.destination}}
        : form;
  }
}

/** The context's flow: its answers, the step shown, and the form they name. */
function flowOf(context: NewRunContext): Flow {
  return {answers: context.answers, at: context.at, form: context.form};
}

/** The entries of the catalogue of the machine the form is for; empty while it is not known. */
function entriesOf(context: NewRunContext): readonly CatalogueEntry[] {
  return catalogueFor(context.examples, context.form.machine) ?? [];
}

/** The parts of the context for what `machine`'s helper said of its examples. */
function examplesParts(context: NewRunContext, value: ExamplesFor['value']) {
  const examples: ExamplesFor = {for: context.form.machine, value};
  return {
    examples,
    ...fromFlow(flowSettled(flowOf(context), catalogueFor(examples, examples.for))),
  };
}

/** The parts of the context a flow sets. */
function fromFlow(flow: Flow): Pick<NewRunContext, 'answers' | 'at' | 'form'> {
  return {answers: flow.answers, at: flow.at, form: flow.form};
}

/**
 * The context's parts for a form of `machine` with nothing answered, from the runs opened before:
 * the machine's last agent, budget and extra arguments are the form's defaults.
 */
function freshParts(machine: HostKey, recent: readonly RecentRun[]) {
  return {
    ...fromFlow(startFlow(prefill(machine, recent))),
    facts: null,
    helper: null,
    harnesses: null,
    agentDefaulted: false,
    examples: null,
    tasks: null,
    goal: null,
    destination: null,
    compute: null,
    checks: null,
    roles: null,
    rerun: lastRun(machine, recent)?.start ?? null,
    problem: null,
  };
}

/** What a preflight answer changes: the checks for the asked inputs, the roles, and the form. */
function afterPreflight(context: NewRunContext, answer: Offered<PreflightResult>) {
  const asked = inputsKey(context.form) ?? '';
  if (answer.kind !== 'offered') {
    // Only a helper that agreed to `preflight` is asked; none checks nothing.
    return {
      checks: {for: asked, list: [], backend: null},
      roles: context.roles,
      form: context.form,
    };
  }
  const roles =
    answer.value.roles === null ? context.roles : rolesAfter(context.roles, answer.value.roles);
  return {
    checks: {for: asked, list: answer.value.checks, backend: answer.value.backend},
    roles,
    // The form follows the run's roles: a setting for a role it does not have would fail the run.
    form: formWithRoles(context.form, roles),
  };
}

/**
 * The context's parts for the form moved to `machine`: what it offers is asked again, the example
 * the user chose stays, and a project of theirs does not (its path is on the machine they left).
 */
function movedParts(context: NewRunContext, machine: HostKey) {
  const parts = freshParts(machine, context.recent);
  const own = context.answers.from === 'project';
  const answers = own
    ? {...context.answers, project: null, task: null, goalSeen: false}
    : context.answers;
  return {...parts, answers, at: context.at};
}

/** The project made from `example` at `path`. */
function createdProject(
  example: {readonly example: string; readonly destination: string},
  path: string,
) {
  return {
    source: 'created',
    example: example.example,
    destination: example.destination,
    path,
  } as const;
}

/**
 * The parts of the context a fix done sets. It applies only to what it was asked of: an example
 * that is still the one chosen becomes a project of its own, a project of the user's that is still
 * the one chosen is replaced by its copy; if the user has moved on, the copy is left alone.
 */
function fixed(context: NewRunContext, done: FixDone): Partial<NewRunContext> {
  const {fix, created} = done;
  const {form, answers} = context;
  if (fix.kind === 'create_from_example') {
    const made = form.project;
    if (made?.source !== 'example' || made.example !== fix.example) return {};
    if (created === null) {
      // Something is there now (a run that raced this one, a junk directory): say so, as the check does.
      const key = destinationKey(form);
      const taken = {state: 'occupied', project: null} satisfies DestinationFound;
      return key === null ? {} : {destination: {for: key, found: taken}};
    }
    return {form: {...form, project: createdProject(made, created.project)}};
  }
  if (created === null) return {};
  if (answers.project?.path !== fix.source) return {};
  const copy = {source: 'folder', path: created.project} as const;
  return {answers: {...answers, project: copy}, form: {...form, project: copy}};
}

/**
 * The flow once the tasks of the user's project are read: the task answered stays when the project
 * has it, else it is unanswered; a project with only one has it answered.
 */
function flowWithTasks(context: NewRunContext, list: readonly string[]): Flow {
  const flow = flowOf(context);
  const named = flow.answers.task;
  const only = list.length === 1 ? list[0] : undefined;
  if (named !== null && !list.includes(named)) {
    const answers = {...flow.answers, task: only ?? null, goalSeen: false};
    return flowSettled(
      {...flow, answers, form: {...flow.form, task: only ?? null}},
      entriesOf(context),
    );
  }
  if (named === null && only !== undefined) {
    return flowAnswer(flow, {kind: 'task', task: only}, false, entriesOf(context));
  }
  return flow;
}

/** The project and task whose goal is asked: the user's own, once both are answered. */
function goalAsked(
  context: NewRunContext,
): {readonly project: string; readonly task: string} | null {
  const project = projectPath(context.form);
  const {task} = context.answers;
  return context.answers.from === 'project' && project !== null && task !== null
    ? {project, task}
    : null;
}

/** Whether the machine reads the tasks of the user's own project now. */
function readsTasks(context: NewRunContext): boolean {
  return context.answers.from === 'project' && projectPath(context.form) !== null;
}

/**
 * The context's parts once the host listed its harnesses: the offers with the installed ones
 * first (when the machine has said which), and the agent the form starts with. An agent already
 * in the form stays while still offered; a past run's stays when offered; else the first offer,
 * which is the first installed one.
 */
function withHarnesses(
  context: NewRunContext,
  listed: readonly HarnessOffer[],
): Partial<NewRunContext> {
  const machine = context.form.machine;
  const list = installedFirst(listed, installedHarnesses(context.facts, machine));
  const kept = context.form.agent;
  const keeps =
    kept !== null &&
    list.some(offer => offer.provider === kept.provider && offer.model === kept.model);
  const agent = keeps ? kept : recordedAgent(context.rerun, list);
  const flow = {...flowOf(context), form: {...context.form, agent}};
  return {
    harnesses: {for: machine, listed, list},
    agentDefaulted: keeps ? context.agentDefaulted : !recordedHarnessOffered(context.rerun, list),
    ...fromFlow(flowSettled(flow, catalogueFor(context.examples, machine))),
  };
}

/**
 * The context's parts once the machine described itself: the harness offers reordered, and the
 * agent moved to the first installed one when the form's is the machine's own pick, is not
 * installed, and another offered harness is. `moved` says the form's agent changed.
 */
function withFacts(
  context: NewRunContext,
  facts: FactsFor,
): {readonly parts: Partial<NewRunContext>; readonly moved: boolean} {
  const machine = context.form.machine;
  const harnesses = context.harnesses;
  if (harnesses === null || harnesses.for !== machine) return {parts: {facts}, moved: false};
  const installed = installedHarnesses(facts, machine);
  const list = installedFirst(harnesses.listed, installed);
  const reordered = {...context, facts, harnesses: {...harnesses, list}};
  const agent = context.form.agent;
  const first = list[0];
  const chosen =
    context.agentDefaulted &&
    agent !== null &&
    installed !== null &&
    !installed.includes(agent.provider) &&
    first !== undefined &&
    installed.includes(first.provider)
      ? defaultAgent(list, [], first.provider)
      : null;
  if (chosen === null) return {parts: {facts, harnesses: reordered.harnesses}, moved: false};
  const form = withInput(reordered, {
    type: 'agent',
    provider: chosen.provider,
    model: chosen.model,
  });
  return {parts: {facts, harnesses: reordered.harnesses, form}, moved: true};
}

export const newRunMachine = setup({
  types: {
    context: {} as NewRunContext,
    events: {} as NewRunEvent,
    input: {} as NewRunInput,
    tags: {} as typeof READING,
  },
  actors: {
    describe: unprovided<Offered<MachineDescription>, {readonly host: HostKey}>('describe'),
    features: unprovided<readonly HelperFeature[], {readonly host: HostKey}>('features'),
    harnesses: unprovided<readonly HarnessOffer[], {readonly host: HostKey}>('harnesses'),
    examples: unprovided<Offered<ExampleList>, {readonly host: HostKey}>('examples'),
    tasks: unprovided<readonly string[], {readonly host: HostKey; readonly project: string}>(
      'tasks',
    ),
    goal: unprovided<
      Offered<TaskGoal>,
      {readonly host: HostKey; readonly project: string; readonly task: string}
    >('goal'),
    preflight: unprovided<
      Offered<PreflightResult>,
      {readonly host: HostKey; readonly inputs: RunInputs}
    >('preflight'),
    destination: unprovided<
      Offered<DestinationFound>,
      {readonly host: HostKey; readonly example: string; readonly destination: string}
    >('destination'),
    fix: unprovided<FixDone, {readonly host: HostKey; readonly fix: ProjectFix}>('fix'),
    start: unprovided<StartedRun, {readonly host: HostKey; readonly inputs: RunInputs}>('start'),
  },
  guards: {
    readsTasks: ({context}) => readsTasks(context),
    readsGoal: ({context}) =>
      goalAsked(context) !== null && offers(context.helper, context.form.machine, 'project_goal'),
    // A new project, task or path to read from changes which goal is asked.
    changesGoal: ({event}) =>
      event.type === 'answer' && ['project', 'task', 'from'].includes(event.change.kind),
    hasInputs: ({context}) => inputsKey(context.form) !== null,
    // The destination of an example not made yet is asked of a helper that can say what it holds.
    asksDestination: ({context}) =>
      destinationKey(context.form) !== null &&
      offers(context.helper, context.form.machine, 'example_destination'),
    // The project to use is the example's the form has chosen and not made.
    usesExample: ({context, event}) =>
      event.type === 'use_created' &&
      context.form.project?.source === 'example' &&
      context.form.project.example === event.example,
    mayStart: ({context}) =>
      checksPass(context.form, context.checks) &&
      missingFeatures(context.helper, context.form.machine).length === 0 &&
      // A start that may have happened is not repeated before the person has looked.
      !startUnconfirmed(context.problem),
    unconfirmedStart: ({context}) => startUnconfirmed(context.problem),
    helperKnown: ({context}) => context.helper?.for === context.form.machine,
    offersChecks: ({context}) =>
      offers(context.helper, context.form.machine, 'preflight') &&
      offers(context.helper, context.form.machine, 'run_options'),
    offersExamples: ({context}) => offers(context.helper, context.form.machine, 'examples'),
    // The roles the run reported change what is asked: check the inputs again.
    // The failed step is the one a region would run again on `retry`.
    failedAt: ({context}, params: {readonly step: Step}) => context.problem?.step === params.step,
    rolesChangeInputs: ({context}, params: {readonly answer: Offered<PreflightResult>}) =>
      inputsKey(afterPreflight(context, params.answer).form) !== inputsKey(context.form),
    // A fix is offered for the project the form has: an example to make, or a project to copy.
    fixApplies: ({context, event}) => {
      if (event.type !== 'fix') return false;
      const {fix} = event;
      return fix.kind === 'create_from_example'
        ? context.form.project?.source === 'example' && context.form.project.example === fix.example
        : context.answers.project?.path === fix.source;
    },
    // An answer to the project question, or a change of path, changes which tasks are read.
    answersProject: ({event}) =>
      event.type === 'answer' && (event.change.kind === 'project' || event.change.kind === 'from'),
  },
  delays: {debounce: CHECK_DEBOUNCE_MS, examplesBound: EXAMPLES_BOUND_MS},
  actions: {
    clearProblem: assign({problem: null}),
    fail: assign((_, params: {readonly step: Step; readonly error: unknown}) => ({
      problem: problemOf(params.step, params.error),
    })),
    // An input changes in the checks region, which every input restarts: an event a region handles
    // is not also handled by the parallel state above it.
    input: assign(({context, event}) => ({
      form: withInput(context, event as InputEvent),
      problem: null,
      // A harness the person picked stays whatever the machine reports installed.
      ...(event.type === 'agent' ? {agentDefaulted: false} : {}),
    })),
    // What the machine has set up changed: a target it no longer offers is dropped.
    compute: assign(({context, event}) => {
      const {machine, compute} = event as Extract<NewRunEvent, {type: 'compute'}>;
      if (machine !== context.form.machine) return {};
      return {compute, form: formWithMeasure(context.form, compute)};
    }),
    answer: assign(({context, event}) => {
      const {change, advance} = event as Extract<NewRunEvent, {type: 'answer'}>;
      return {
        ...fromFlow(flowAnswer(flowOf(context), change, advance === true, entriesOf(context))),
        problem: null,
      };
    }),
  },
}).createMachine({
  id: 'newRun',
  context: ({input}) => ({
    ...freshParts(input.machine, input.recent),
    ...(input.draft === undefined || input.draft === null
      ? {}
      : fromFlow(flowOfDraft(input.draft, []))),
    recent: input.recent,
    started: null,
    moreOpen: input.moreOpen,
  }),
  initial: 'editing',
  states: {
    editing: {
      type: 'parallel',
      on: {
        machine: {
          target: 'editing',
          reenter: true,
          actions: assign(({context, event}) => movedParts(context, event.machine)),
        },
        // Run again: the run's place and options, its agent once the harnesses are known.
        again: {
          target: 'editing',
          reenter: true,
          actions: assign(({context, event}) => ({
            ...freshParts(event.run.host, context.recent),
            ...fromFlow({
              answers: againAnswers(event.run),
              at: 'review',
              form: againForm(event.run),
            }),
            rerun: event.run.start ?? null,
          })),
        },
        reset: {
          target: 'editing',
          reenter: true,
          actions: assign(({context}) => freshParts(context.form.machine, context.recent)),
        },
        go: {
          actions: assign(({context, event}) =>
            fromFlow(flowGo(flowOf(context), event.step, entriesOf(context))),
          ),
        },
        next: {
          actions: assign(({context}) => fromFlow(flowNext(flowOf(context), entriesOf(context)))),
        },
        back: {
          actions: assign(({context}) => fromFlow(flowBack(flowOf(context), entriesOf(context)))),
        },
        recent: {actions: assign(({event}) => ({recent: event.recent}))},
        more_options: {
          actions: assign(({context, event}) => ({
            moreOpen: {...context.moreOpen, [event.machine]: event.open},
          })),
        },
      },
      states: {
        helper: {
          initial: 'loading',
          on: {
            retry: {
              guard: {type: 'failedAt', params: {step: 'features'}},
              target: '.loading',
              actions: 'clearProblem',
            },
          },
          states: {
            loading: {
              tags: READING,
              invoke: {
                id: 'features',
                src: 'features',
                input: ({context}) => ({host: context.form.machine}),
                onDone: {
                  // What may be asked changed: check the inputs again.
                  target: 'ready',
                  actions: [
                    assign(({context, event}) => ({
                      helper: {for: context.form.machine, features: event.output},
                    })),
                    raise({type: 'recheck'}),
                  ],
                },
                onError: {
                  target: 'ready',
                  actions: [
                    {
                      type: 'fail',
                      params: ({event}) => ({step: 'features', error: event.error}),
                    },
                    raise({type: 'helper_failed'}),
                  ],
                },
              },
            },
            ready: {},
          },
        },
        facts: {
          initial: 'loading',
          on: {
            retry: {
              guard: {type: 'failedAt', params: {step: 'describe'}},
              target: '.loading',
              actions: 'clearProblem',
            },
          },
          states: {
            loading: {
              tags: READING,
              invoke: {
                id: 'describe',
                src: 'describe',
                input: ({context}) => ({host: context.form.machine}),
                onDone: [
                  {
                    // The first installed harness replaces the machine's own pick of an
                    // uninstalled one: the inputs changed, so check them again.
                    guard: ({context, event}) =>
                      withFacts(context, {for: context.form.machine, value: event.output}).moved,
                    target: 'ready',
                    actions: [
                      assign(
                        ({context, event}) =>
                          withFacts(context, {for: context.form.machine, value: event.output})
                            .parts,
                      ),
                      raise({type: 'recheck'}),
                    ],
                  },
                  {
                    target: 'ready',
                    actions: assign(
                      ({context, event}) =>
                        withFacts(context, {for: context.form.machine, value: event.output}).parts,
                    ),
                  },
                ],
                onError: {
                  target: 'ready',
                  actions: {
                    type: 'fail',
                    params: ({event}) => ({step: 'describe', error: event.error}),
                  },
                },
              },
            },
            ready: {},
          },
        },
        harnesses: {
          initial: 'loading',
          on: {
            retry: {
              guard: {type: 'failedAt', params: {step: 'harnesses'}},
              target: '.loading',
              actions: 'clearProblem',
            },
          },
          states: {
            loading: {
              tags: READING,
              invoke: {
                id: 'harnesses',
                src: 'harnesses',
                input: ({context}) => ({host: context.form.machine}),
                onDone: {
                  // The default agent is part of the inputs: check them again.
                  target: 'ready',
                  actions: [
                    assign(({context, event}) => withHarnesses(context, event.output)),
                    raise({type: 'recheck'}),
                  ],
                },
                onError: {
                  target: 'ready',
                  actions: {
                    type: 'fail',
                    params: ({event}) => ({step: 'harnesses', error: event.error}),
                  },
                },
              },
            },
            ready: {},
          },
        },
        examples: {
          initial: 'pending',
          on: {
            retry: {
              guard: {type: 'failedAt', params: {step: 'examples'}},
              target: '.pending',
              actions: ['clearProblem', assign({examples: null})],
            },
          },
          states: {
            // A helper without `examples` is never asked: it offers none. One that cannot be
            // reached, or never answers, offers none either: the screen says so.
            pending: {
              tags: READING,
              always: [
                {guard: 'offersExamples', target: 'loading'},
                {
                  guard: 'helperKnown',
                  target: 'ready',
                  actions: assign(({context}) => examplesParts(context, {kind: 'unsupported'})),
                },
              ],
              after: {
                examplesBound: {
                  target: 'ready',
                  actions: assign(({context}) => examplesParts(context, {kind: 'failed'})),
                },
              },
              on: {
                helper_failed: {
                  target: 'ready',
                  actions: assign(({context}) => examplesParts(context, {kind: 'failed'})),
                },
              },
            },
            loading: {
              tags: READING,
              after: {
                examplesBound: {
                  target: 'ready',
                  actions: assign(({context}) => examplesParts(context, {kind: 'failed'})),
                },
              },
              invoke: {
                id: 'examples',
                src: 'examples',
                input: ({context}) => ({host: context.form.machine}),
                onDone: {
                  target: 'ready',
                  actions: assign(({context, event}) => examplesParts(context, event.output)),
                },
                onError: {
                  target: 'ready',
                  actions: [
                    assign(({context}) => examplesParts(context, {kind: 'failed'})),
                    {
                      type: 'fail',
                      params: ({event}) => ({step: 'examples', error: event.error}),
                    },
                  ],
                },
              },
            },
            ready: {},
          },
        },
        tasks: {
          initial: 'choosing',
          // Each region restarts on what it depends on: a transition from the parallel state itself
          // would leave and re-enter every region, stopping all their work.
          on: {
            answer: {guard: 'answersProject', target: '.choosing'},
            refresh: '.choosing',
            retry: {
              guard: {type: 'failedAt', params: {step: 'tasks'}},
              target: '.choosing',
              actions: 'clearProblem',
            },
          },
          states: {
            choosing: {
              always: [{guard: 'readsTasks', target: 'loading'}, {target: 'none'}],
            },
            none: {},
            loading: {
              tags: READING,
              invoke: {
                id: 'tasks',
                src: 'tasks',
                input: ({context}) => ({
                  host: context.form.machine,
                  project: projectPath(context.form) ?? '',
                }),
                onDone: {
                  // The task kept or chosen from the list is part of the inputs: check them again.
                  target: 'ready',
                  actions: [
                    assign(({context, event}) => ({
                      tasks: {for: projectPath(context.form) ?? '', list: event.output},
                      ...fromFlow(flowWithTasks(context, event.output)),
                    })),
                    raise({type: 'recheck'}),
                  ],
                },
                onError: {
                  target: 'ready',
                  actions: {
                    type: 'fail',
                    params: ({event}) => ({step: 'tasks', error: event.error}),
                  },
                },
              },
            },
            ready: {},
          },
        },
        goal: {
          initial: 'choosing',
          on: {
            answer: {guard: 'changesGoal', target: '.choosing'},
            retry: {
              guard: {type: 'failedAt', params: {step: 'goal'}},
              target: '.choosing',
              actions: 'clearProblem',
            },
          },
          states: {
            choosing: {always: [{guard: 'readsGoal', target: 'loading'}, {target: 'waiting'}]},
            // Until the helper says it can read a goal, there is nothing to ask.
            waiting: {always: {guard: 'readsGoal', target: 'loading'}},
            loading: {
              tags: READING,
              invoke: {
                id: 'goal',
                src: 'goal',
                input: ({context}) => ({
                  host: context.form.machine,
                  ...(goalAsked(context) as {project: string; task: string}),
                }),
                onDone: {
                  target: 'ready',
                  actions: assign(({context, event}) => {
                    const asked = goalAsked(context);
                    return asked === null
                      ? {}
                      : {
                          goal: {
                            for: goalKey(context.form.machine, asked.project, asked.task),
                            value: event.output,
                          } satisfies GoalFor,
                        };
                  }),
                },
                onError: {
                  target: 'ready',
                  actions: {
                    type: 'fail',
                    params: ({event}) => ({step: 'goal', error: event.error}),
                  },
                },
              },
            },
            ready: {},
          },
        },
        destination: {
          initial: 'choosing',
          on: {
            // Any answer may choose another example, so the destination is read again.
            answer: '.choosing',
            // A destination typed is asked once the typing pauses.
            destination: '.typing',
            refresh: '.choosing',
          },
          states: {
            choosing: {
              always: [{guard: 'asksDestination', target: 'loading'}, {target: 'waiting'}],
            },
            // Until the helper says it can read a destination, there is nothing to ask.
            waiting: {always: {guard: 'asksDestination', target: 'loading'}},
            typing: {after: {debounce: 'choosing'}},
            loading: {
              tags: READING,
              invoke: {
                id: 'destination',
                src: 'destination',
                input: ({context}) => {
                  const {project} = context.form;
                  return {
                    host: context.form.machine,
                    example: project?.source === 'example' ? project.example : '',
                    destination: project?.source === 'example' ? project.destination : '',
                  };
                },
                onDone: {
                  target: 'ready',
                  actions: assign(({context, event}) => {
                    const key = destinationKey(context.form);
                    return key === null
                      ? {}
                      : {
                          destination: {
                            for: key,
                            found: event.output.kind === 'offered' ? event.output.value : null,
                          } satisfies DestinationFor,
                        };
                  }),
                },
                // A question that failed is not a reason to refuse: creating says what is there.
                onError: {
                  target: 'ready',
                  actions: assign(({context}) => {
                    const key = destinationKey(context.form);
                    return key === null ? {} : {destination: {for: key, found: null}};
                  }),
                },
              },
            },
            ready: {},
          },
        },
        checks: {
          initial: 'deciding',
          on: {
            answer: {target: '.deciding', actions: 'answer'},
            agent: {target: '.deciding', actions: 'input'},
            role: {target: '.deciding', actions: 'input'},
            budget: {target: '.deciding', actions: 'input'},
            measure: {target: '.deciding', actions: 'input'},
            compute: {target: '.deciding', actions: 'compute'},
            advanced: {target: '.deciding', actions: 'input'},
            destination: {target: '.deciding', actions: 'input'},
            recheck: '.deciding',
            refresh: '.deciding',
            retry: {
              guard: {type: 'failedAt', params: {step: 'preflight'}},
              target: '.deciding',
              actions: 'clearProblem',
            },
          },
          states: {
            // Whether the inputs are to be checked is decided where they change, so that only
            // inputs that will be checked wait for the debounce and the answer, and a form with
            // none is at rest at once.
            deciding: {
              always: [
                {guard: and(['hasInputs', 'offersChecks']), target: 'waiting'},
                {target: 'incomplete'},
              ],
            },
            waiting: {
              tags: READING,
              // Checked only on a helper that takes the checks and typed options.
              after: {
                debounce: [
                  {guard: and(['hasInputs', 'offersChecks']), target: 'checking'},
                  {target: 'incomplete'},
                ],
              },
            },
            incomplete: {},
            checking: {
              tags: READING,
              invoke: {
                id: 'preflight',
                src: 'preflight',
                input: ({context}) => ({
                  host: context.form.machine,
                  inputs: runInputs(context.form) as RunInputs,
                }),
                onDone: [
                  {
                    guard: {
                      type: 'rolesChangeInputs',
                      params: ({event}) => ({answer: event.output}),
                    },
                    target: 'deciding',
                    actions: assign(({context, event}) => afterPreflight(context, event.output)),
                  },
                  {
                    target: 'ready',
                    actions: assign(({context, event}) => afterPreflight(context, event.output)),
                  },
                ],
                onError: {
                  target: 'ready',
                  actions: {
                    type: 'fail',
                    params: ({event}) => ({step: 'preflight', error: event.error}),
                  },
                },
              },
            },
            ready: {},
          },
        },
        action: {
          initial: 'idle',
          states: {
            idle: {
              on: {
                fix: {guard: 'fixApplies', target: 'fixing'},
                use_created: {
                  guard: 'usesExample',
                  target: '#newRun.editing',
                  reenter: true,
                  actions: assign(({context, event}) => {
                    const {form} = context;
                    const made = form.project;
                    if (made?.source !== 'example') return {};
                    return {
                      form: {...form, project: createdProject(made, event.path)},
                      checks: null,
                      problem: null,
                    };
                  }),
                },
                start: {guard: 'mayStart', target: 'starting'},
                looked: {guard: 'unconfirmedStart', actions: 'clearProblem'},
              },
            },
            fixing: {
              invoke: {
                id: 'fix',
                src: 'fix',
                input: ({context, event}) => ({
                  host: context.form.machine,
                  fix: (event as Extract<NewRunEvent, {type: 'fix'}>).fix,
                }),
                onDone: [
                  // A destination that holds something stays on this page, saying so.
                  {
                    guard: ({event}) => event.output.created === null,
                    target: 'idle',
                    actions: assign(({context, event}) => ({
                      ...fixed(context, event.output),
                      problem: null,
                    })),
                  },
                  {
                    target: '#newRun.editing',
                    reenter: true,
                    actions: assign(({context, event}) => ({
                      ...fixed(context, event.output),
                      checks: null,
                      problem: null,
                    })),
                  },
                ],
                onError: {
                  target: 'idle',
                  actions: {type: 'fail', params: ({event}) => ({step: 'fix', error: event.error})},
                },
              },
            },
            starting: {
              invoke: {
                id: 'start',
                src: 'start',
                input: ({context}) => ({
                  host: context.form.machine,
                  inputs: runInputs(context.form) as RunInputs,
                }),
                onDone: {
                  target: '#newRun.started',
                  actions: assign(({event}) => ({started: event.output})),
                },
                onError: {
                  target: 'idle',
                  actions: {
                    type: 'fail',
                    params: ({event}) => ({step: 'start', error: event.error}),
                  },
                },
              },
            },
          },
        },
      },
    },
    started: {type: 'final'},
  },
});

/** What the screen renders: the machine's snapshot. */
export type NewRunSnapshot = SnapshotFrom<typeof newRunMachine>;

/**
 * Whether a read the screen started is still unanswered, so that what the screen shows (its
 * examples, the goal, the machine's agents) may still change without an input of the user. A read
 * that failed has answered: the screen shows its problem.
 */
export function isReading(snapshot: NewRunSnapshot): boolean {
  return snapshot.hasTag(READING);
}
