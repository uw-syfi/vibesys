/**
 * The New run screen's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"); the form is data in its context. While editing, parallel regions load what the machine
 * offers (its facts, vetted harnesses, examples), the project's tasks, and the "Ready to start"
 * checks for the current inputs, debounced; one more region runs a fix or the start. Changing an
 * input re-enters the regions that depend on it, which stops their work in flight, and a reply is
 * applied only for the inputs it was asked for, so a slow reply never overwrites newer state.
 * Start is enabled only when the checks are for the current inputs and none fails.
 */
import type {
  CreatedProject,
  ExampleList,
  HelperFeature,
  MachineDescription,
} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';
import type {HarnessOffer, Offered, PreflightResult, RunInputs, StartedRun} from '@vibesys/ipc';
import {unprovided} from '@vibesys/machine';
import type {RecentRun, RunStart} from '@vibesys/runs';
import {and, assign, raise, type SnapshotFrom, setup} from 'xstate';
import {
  type AgentChoice,
  againForm,
  type Budget,
  type ChecksFor,
  checksPass,
  defaultAgent,
  type FactsFor,
  type Form,
  type HelperFor,
  inputsKey,
  lastRun,
  missingFeatures,
  offers,
  type ProjectChoice,
  prefill,
  projectPath,
  type RoleChoice,
  recordedAgent,
  runInputs,
} from './form.js';
import {formWithRoles, rolesAfter} from './roles.js';

/** The fixes the screen performs itself; the rest are the shell's (Machines) or the user's. */
export type ProjectFix =
  | {readonly kind: 'copy_out'; readonly source: string; readonly destination: string}
  | {readonly kind: 'create_from_example'; readonly example: string; readonly destination: string};

export interface NewRunContext {
  readonly form: Form;
  readonly recent: readonly RecentRun[];
  readonly facts: FactsFor | null;
  /** What the machine's helper agreed to: New run asks only what it takes. */
  readonly helper: HelperFor | null;
  readonly harnesses: {readonly for: HostKey; readonly list: readonly HarnessOffer[]} | null;
  readonly examples: {readonly for: HostKey; readonly value: Offered<ExampleList>} | null;
  readonly tasks: {readonly for: string; readonly list: readonly string[]} | null;
  readonly checks: ChecksFor | null;
  /**
   * The role ids the run's orchestration declares, from the latest preflight that reported any:
   * null before one did, or from a helper that does not report them (`offers(..., 'run_roles')`).
   */
  readonly roles: readonly string[] | null;
  /** What the run being run again was started with: its agent is chosen from it once known. */
  readonly rerun: RunStart | null;
  /** Why the last load, fix or start failed, until the next input. */
  readonly problem: string | null;
  readonly started: StartedRun | null;
}

export type NewRunEvent =
  | {readonly type: 'machine'; readonly machine: HostKey}
  | {readonly type: 'again'; readonly run: RecentRun}
  | {readonly type: 'project'; readonly project: ProjectChoice}
  | {readonly type: 'task'; readonly task: string}
  | {readonly type: 'provider'; readonly provider: string}
  | {readonly type: 'model'; readonly model: string}
  | {readonly type: 'role'; readonly role: RoleChoice}
  | {readonly type: 'budget'; readonly budget: Budget}
  | {readonly type: 'advanced'; readonly advanced: string}
  | {readonly type: 'recent'; readonly recent: readonly RecentRun[]}
  | {readonly type: 'recheck'}
  | {readonly type: 'fix'; readonly fix: ProjectFix}
  | {readonly type: 'start'};

export interface NewRunInput {
  readonly machine: HostKey;
  readonly recent: readonly RecentRun[];
}

/** How long the inputs must stay still before they are checked, in milliseconds. */
export const CHECK_DEBOUNCE_MS = 400;

function withProject(form: Form, project: ProjectChoice): Form {
  return {...form, project, task: null};
}

function withAgent(form: Form, change: (agent: AgentChoice) => AgentChoice): Form {
  return form.agent === null ? form : {...form, agent: change(form.agent)};
}

/** The events that change one input of the form. */
type InputEvent = Extract<
  NewRunEvent,
  {readonly type: 'project' | 'task' | 'provider' | 'model' | 'role' | 'budget' | 'advanced'}
>;

/** The form after `event` changed one of its inputs. */
function withInput(context: NewRunContext, event: InputEvent): Form {
  const {form} = context;
  switch (event.type) {
    case 'project':
      return withProject(form, event.project);
    case 'task':
      return {...form, task: event.task};
    case 'provider':
      return {
        ...form,
        agent: defaultAgent(context.harnesses?.list ?? [], context.roles ?? [], event.provider),
      };
    case 'model':
      return withAgent(form, agent => ({...agent, model: event.model}));
    case 'role':
      return withAgent(form, agent => ({
        ...agent,
        roles: agent.roles.map(role => (role.role === event.role.role ? event.role : role)),
      }));
    case 'budget':
      return {...form, budget: event.budget};
    case 'advanced':
      return {...form, advanced: event.advanced};
  }
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

/** The task to pick from `list`: the one already chosen when it is offered, else the first. */
function chosenTask(form: Form, list: readonly string[]): string | null {
  return form.task !== null && list.includes(form.task) ? form.task : (list[0] ?? null);
}

export const newRunMachine = setup({
  types: {
    context: {} as NewRunContext,
    events: {} as NewRunEvent,
    input: {} as NewRunInput,
  },
  actors: {
    describe: unprovided<Offered<MachineDescription>, {readonly host: HostKey}>('describe'),
    features: unprovided<readonly HelperFeature[], {readonly host: HostKey}>('features'),
    harnesses: unprovided<readonly HarnessOffer[], {readonly host: HostKey}>('harnesses'),
    examples: unprovided<Offered<ExampleList>, {readonly host: HostKey}>('examples'),
    tasks: unprovided<readonly string[], {readonly host: HostKey; readonly project: string}>(
      'tasks',
    ),
    preflight: unprovided<
      Offered<PreflightResult>,
      {readonly host: HostKey; readonly inputs: RunInputs}
    >('preflight'),
    fix: unprovided<CreatedProject, {readonly host: HostKey; readonly fix: ProjectFix}>('fix'),
    start: unprovided<StartedRun, {readonly host: HostKey; readonly inputs: RunInputs}>('start'),
  },
  guards: {
    hasProject: ({context}) => projectPath(context.form) !== null,
    hasInputs: ({context}) => inputsKey(context.form) !== null,
    mayStart: ({context}) =>
      checksPass(context.form, context.checks) &&
      missingFeatures(context.helper, context.form.machine).length === 0,
    helperKnown: ({context}) => context.helper?.for === context.form.machine,
    offersChecks: ({context}) =>
      offers(context.helper, context.form.machine, 'preflight') &&
      offers(context.helper, context.form.machine, 'run_options'),
    offersExamples: ({context}) => offers(context.helper, context.form.machine, 'examples'),
    // The roles the run reported change what is asked: check the inputs again.
    rolesChangeInputs: ({context}, params: {readonly answer: Offered<PreflightResult>}) =>
      inputsKey(afterPreflight(context, params.answer).form) !== inputsKey(context.form),
  },
  delays: {debounce: CHECK_DEBOUNCE_MS},
  actions: {
    fail: assign((_, params: {readonly error: unknown}) => ({
      problem: params.error instanceof Error ? params.error.message : String(params.error),
    })),
    // An input changes in the checks region, which every input restarts: an event a region handles
    // is not also handled by the parallel state above it.
    input: assign(({context, event}) => ({
      form: withInput(context, event as InputEvent),
      problem: null,
    })),
  },
}).createMachine({
  id: 'newRun',
  context: ({input}) => ({
    form: prefill(input.machine, input.recent),
    recent: input.recent,
    facts: null,
    helper: null,
    harnesses: null,
    examples: null,
    tasks: null,
    checks: null,
    roles: null,
    rerun: lastRun(input.machine, input.recent)?.start ?? null,
    problem: null,
    started: null,
  }),
  initial: 'editing',
  states: {
    editing: {
      type: 'parallel',
      on: {
        machine: {
          target: 'editing',
          reenter: true,
          actions: assign(({context, event}) => ({
            form: prefill(event.machine, context.recent),
            facts: null,
            helper: null,
            harnesses: null,
            examples: null,
            tasks: null,
            checks: null,
            roles: null,
            rerun: lastRun(event.machine, context.recent)?.start ?? null,
            problem: null,
          })),
        },
        // Run again: the run's place and options, its agent once the harnesses are known.
        again: {
          target: 'editing',
          reenter: true,
          actions: assign(({event}) => ({
            form: againForm(event.run),
            rerun: event.run.start ?? null,
            facts: null,
            helper: null,
            harnesses: null,
            examples: null,
            tasks: null,
            checks: null,
            roles: null,
            problem: null,
          })),
        },
        recent: {actions: assign(({event}) => ({recent: event.recent}))},
      },
      states: {
        helper: {
          initial: 'loading',
          states: {
            loading: {
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
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
                },
              },
            },
            ready: {},
          },
        },
        facts: {
          initial: 'loading',
          states: {
            loading: {
              invoke: {
                id: 'describe',
                src: 'describe',
                input: ({context}) => ({host: context.form.machine}),
                onDone: {
                  target: 'ready',
                  actions: assign(({context, event}) => ({
                    facts: {for: context.form.machine, value: event.output},
                  })),
                },
                onError: {
                  target: 'ready',
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
                },
              },
            },
            ready: {},
          },
        },
        harnesses: {
          initial: 'loading',
          states: {
            loading: {
              invoke: {
                id: 'harnesses',
                src: 'harnesses',
                input: ({context}) => ({host: context.form.machine}),
                onDone: {
                  // The default agent is part of the inputs: check them again.
                  target: 'ready',
                  actions: [
                    assign(({context, event}) => ({
                      harnesses: {for: context.form.machine, list: event.output},
                      form: {
                        ...context.form,
                        agent: context.form.agent ?? recordedAgent(context.rerun, event.output),
                      },
                    })),
                    raise({type: 'recheck'}),
                  ],
                },
                onError: {
                  target: 'ready',
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
                },
              },
            },
            ready: {},
          },
        },
        examples: {
          initial: 'pending',
          states: {
            // A helper without `examples` is never asked: it offers none.
            pending: {
              always: [
                {guard: 'offersExamples', target: 'loading'},
                {
                  guard: 'helperKnown',
                  target: 'ready',
                  actions: assign(({context}) => ({
                    examples: {for: context.form.machine, value: {kind: 'unsupported'}},
                  })),
                },
              ],
            },
            loading: {
              invoke: {
                id: 'examples',
                src: 'examples',
                input: ({context}) => ({host: context.form.machine}),
                onDone: {
                  target: 'ready',
                  actions: assign(({context, event}) => ({
                    examples: {for: context.form.machine, value: event.output},
                  })),
                },
                onError: {
                  target: 'ready',
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
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
          on: {project: '.choosing'},
          states: {
            choosing: {
              always: [{guard: 'hasProject', target: 'loading'}, {target: 'none'}],
            },
            none: {},
            loading: {
              invoke: {
                id: 'tasks',
                src: 'tasks',
                input: ({context}) => ({
                  host: context.form.machine,
                  project: projectPath(context.form) ?? '',
                }),
                onDone: {
                  // The task chosen from the list is part of the inputs: check them again.
                  target: 'ready',
                  actions: [
                    assign(({context, event}) => ({
                      tasks: {for: projectPath(context.form) ?? '', list: event.output},
                      form: {...context.form, task: chosenTask(context.form, event.output)},
                    })),
                    raise({type: 'recheck'}),
                  ],
                },
                onError: {
                  target: 'ready',
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
                },
              },
            },
            ready: {},
          },
        },
        checks: {
          initial: 'waiting',
          on: {
            project: {target: '.waiting', actions: 'input'},
            task: {target: '.waiting', actions: 'input'},
            provider: {target: '.waiting', actions: 'input'},
            model: {target: '.waiting', actions: 'input'},
            role: {target: '.waiting', actions: 'input'},
            budget: {target: '.waiting', actions: 'input'},
            advanced: {target: '.waiting', actions: 'input'},
            recheck: '.waiting',
          },
          states: {
            waiting: {
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
                    target: 'waiting',
                    actions: assign(({context, event}) => afterPreflight(context, event.output)),
                  },
                  {
                    target: 'ready',
                    actions: assign(({context, event}) => afterPreflight(context, event.output)),
                  },
                ],
                onError: {
                  target: 'ready',
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
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
                fix: 'fixing',
                start: {guard: 'mayStart', target: 'starting'},
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
                onDone: {
                  target: '#newRun.editing',
                  reenter: true,
                  actions: assign(({context, event}) => ({
                    form: withProject(context.form, {source: 'folder', path: event.output.project}),
                    checks: null,
                    problem: null,
                  })),
                },
                onError: {
                  target: 'idle',
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
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
                  actions: {type: 'fail', params: ({event}) => ({error: event.error})},
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
