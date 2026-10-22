/**
 * The New run feature in the main process: it answers the screen's requests with one host helper
 * method each, over the host's connection, and records a started run in Recent.
 */
import {ConnectionError, type ConnectionLease, HelperRequestError} from '@vibesys/connection';
import type {
  Backend,
  Check,
  CreatedProject,
  DetachedLaunchFailure,
  ExampleList,
  HelperFeature,
  HelperRequest,
  LiveInstanceRecord,
  MachineDescription,
  TaskGoal,
} from '@vibesys/contracts/host-helper';
import {type HostId, hostKey, hostLabel} from '@vibesys/hosts';
import type {
  DestinationFound,
  ExampleCreation,
  Offered,
  PreflightResult,
  RunInputs,
  StartedRun,
  WelcomeRequest,
} from '@vibesys/ipc';
import type {StoredRecentRun} from '@vibesys/settings';

/** The New run requests a view sends, as `@vibesys/ipc` parsed them. */
export type NewRunMainRequest = Extract<
  WelcomeRequest,
  {
    readonly kind:
      | 'describe-machine'
      | 'preflight'
      | 'examples'
      | 'create-from-example'
      | 'example-destination'
      | 'copy-out'
      | 'start-run'
      | 'helper-features'
      | 'task-goal';
  }
>;

export interface NewRunMainDeps {
  /** A lease on `host`'s helper connection (the hosts feature's). */
  readonly connect: (host: HostId) => Promise<ConnectionLease>;
  /** Record a started run in Recent (the settings store). */
  readonly remember: (run: StoredRecentRun) => Promise<void>;
  readonly now: () => number;
  /** The per-host switch. Absent or false keeps the helper JSON start. */
  readonly transportV2?: (host: HostId) => Promise<boolean>;
  /**
   * The registry start used when `transportV2` is on. The host keeps the
   * admitted run after `signal` aborts.
   */
  readonly registryStart?: (
    host: HostId,
    inputs: RunInputs,
    signal: AbortSignal,
  ) => Promise<{readonly runId: string; readonly instanceId: string}>;
}

/** One helper request without the fields the connection fills in (`request_id`, `deadline_ms`). */
type HelperCall = HelperRequest extends infer Request
  ? Request extends {readonly type: string}
    ? Omit<Request, 'request_id' | 'deadline_ms'>
    : never
  : never;

/** A failure the user reads: the helper's message, prefixed with what was being done. */
function plain(error: unknown, doing: string): Error {
  const detail = error instanceof Error ? error.message : String(error);
  // A connection failure keeps its typed cause, which the screen words apart from the raw text.
  if (error instanceof ConnectionError) return error.reworded(`${doing}: ${detail}`);
  return new Error(`${doing}: ${detail}`);
}

/**
 * The `run.start` and `run.preflight` fields for `inputs`, sending only what is set. An agent per
 * role goes only to a helper that agreed to `run_role_agents`; for any other the request is
 * refused here, since dropping it would start the run on agents the user did not choose.
 */
function runFields(inputs: RunInputs, features: readonly HelperFeature[]) {
  const roleAgents = Object.keys(inputs.agents).length > 0;
  if (roleAgents && !features.includes('run_role_agents')) {
    throw new Error(
      "This machine's VibeSys cannot run a different agent for a role. Update it, or use the run's agent for every role.",
    );
  }
  const measured = inputs.measure_on.kind !== 'same_machine';
  if (measured && !features.includes('measurement_target')) {
    throw new Error(
      "This machine's VibeSys cannot measure on a cluster or cloud. Update it, or measure on this machine.",
    );
  }
  return {
    project: inputs.project,
    task: inputs.task,
    args: [...inputs.args],
    options: inputs.options,
    ...(inputs.harness === null ? {} : {harness: inputs.harness}),
    ...(roleAgents ? {agents: inputs.agents} : {}),
    ...(measured ? {measure_on: inputs.measure_on} : {}),
  };
}

function isFailure(
  launch: LiveInstanceRecord | DetachedLaunchFailure,
): launch is DetachedLaunchFailure {
  return 'outcome' in launch || 'exit_code' in launch;
}

export class NewRunMain {
  readonly #deps: NewRunMainDeps;

  constructor(deps: NewRunMainDeps) {
    this.#deps = deps;
  }

  /** Answer one validated New run request. */
  answer(request: NewRunMainRequest): Promise<unknown> {
    switch (request.kind) {
      case 'describe-machine':
        return this.#offered(
          request.host,
          {type: 'machine.describe'},
          'Could not describe',
          reply => (reply as {machine: MachineDescription}).machine,
        );
      case 'preflight':
        return this.#preflight(request.host, request.inputs);
      case 'examples':
        return this.#examples(request.host);
      case 'create-from-example':
        return this.#createFromExample(request.host, request.example, request.destination);
      case 'example-destination':
        return this.#offered(
          request.host,
          {
            type: 'examples.destination',
            example: request.example,
            destination: request.destination,
          },
          'Could not check the destination',
          (reply): DestinationFound => {
            const {state, project} = reply as DestinationFound;
            return {state, project};
          },
        );
      case 'copy-out':
        return this.#copyOut(request.host, request.source, request.destination);
      case 'start-run':
        return this.#start(request.host, request.inputs);
      case 'helper-features':
        return this.#features(request.host);
      case 'task-goal':
        return this.#offered(
          request.host,
          {type: 'project.task_goal', project: request.project, task: request.task},
          `Could not read the goal of ${request.task}`,
          reply => (reply as {goal: TaskGoal}).goal,
        );
    }
  }

  /** What `host`'s helper agreed to in `hello`, once the host is ready. */
  async #features(host: HostId): Promise<readonly HelperFeature[]> {
    try {
      await using lease = await this.#deps.connect(host);
      await lease.connection.ready();
      return [...(lease.connection.features ?? [])];
    } catch (error) {
      throw plain(error, `Could not reach ${hostLabel(host)}`);
    }
  }

  async #request(host: HostId, request: HelperCall): Promise<unknown> {
    await using lease = await this.#deps.connect(host);
    // Awaited here, so the lease is held until the reply arrives.
    return await lease.connection.request(
      request as Parameters<typeof lease.connection.request>[0],
    );
  }

  /**
   * The checks for `inputs`, with the run's roles when the helper agreed to `run_roles`, and its
   * backend (and each failure's typed cause) when it agreed to `run_backend`, and the typed cause
   * of every other failed setting when it agreed to `run_causes`.
   */
  async #preflight(host: HostId, inputs: RunInputs): Promise<Offered<PreflightResult>> {
    try {
      await using lease = await this.#deps.connect(host);
      await lease.connection.ready();
      const rolesToo = lease.connection.features?.includes('run_roles') === true;
      const backendToo = lease.connection.features?.includes('run_backend') === true;
      const cleanToo = lease.connection.features?.includes('project_clean') === true;
      const causesToo = lease.connection.features?.includes('run_causes') === true;
      const reply = (await lease.connection.request({
        type: 'run.preflight',
        ...runFields(inputs, lease.connection.features ?? []),
        ...(rolesToo ? {with_roles: true} : {}),
        ...(backendToo ? {with_backend: true} : {}),
        ...(cleanToo ? {with_project_clean: true} : {}),
        ...(causesToo ? {with_causes: true} : {}),
      })) as {checks: Check[]; roles?: string[] | null; backend?: Backend | null};
      return {
        kind: 'offered',
        value: {
          checks: reply.checks,
          roles: rolesToo ? (reply.roles ?? []) : null,
          backend: backendToo ? (reply.backend ?? null) : null,
        },
      };
    } catch (error) {
      if (error instanceof HelperRequestError && error.code === 'feature_not_negotiated') {
        return {kind: 'unsupported'};
      }
      throw plain(error, `Could not check the run on ${hostLabel(host)}`);
    }
  }

  /**
   * The examples, each with what its tasks are about (domain, model, hardware, objectives, goal)
   * when the helper agreed to `example_catalogue`; a helper that did not lists them plain.
   */
  async #examples(host: HostId): Promise<Offered<ExampleList>> {
    try {
      await using lease = await this.#deps.connect(host);
      await lease.connection.ready();
      const described = lease.connection.features?.includes('example_catalogue') === true;
      const reply = (await lease.connection.request({
        type: 'examples.list',
        ...(described ? {with_catalogue: true} : {}),
      })) as {listing: ExampleList};
      return {kind: 'offered', value: reply.listing};
    } catch (error) {
      if (error instanceof HelperRequestError && error.code === 'feature_not_negotiated') {
        return {kind: 'unsupported'};
      }
      throw plain(error, `Could not list the examples on ${hostLabel(host)}`);
    }
  }

  async #offered<T>(
    host: HostId,
    request: HelperCall,
    doing: string,
    pick: (reply: unknown) => T,
  ): Promise<Offered<T>> {
    try {
      return {kind: 'offered', value: pick(await this.#request(host, request))};
    } catch (error) {
      if (error instanceof HelperRequestError && error.code === 'feature_not_negotiated') {
        return {kind: 'unsupported'};
      }
      throw plain(error, `${doing} on ${hostLabel(host)}`);
    }
  }

  /** The new project, or `taken` when the destination holds something (it is not for the user to read). */
  async #createFromExample(
    host: HostId,
    example: string,
    destination: string,
  ): Promise<ExampleCreation> {
    try {
      const reply = (await this.#request(host, {
        type: 'project.create_from_example',
        example,
        destination,
      })) as {created: CreatedProject};
      return {kind: 'created', created: reply.created};
    } catch (error) {
      if (error instanceof HelperRequestError && error.code === 'destination_exists') {
        return {kind: 'taken'};
      }
      throw plain(error, 'Could not create the project');
    }
  }

  async #copyOut(host: HostId, source: string, destination: string): Promise<CreatedProject> {
    try {
      const reply = (await this.#request(host, {
        type: 'project.copy_out',
        source,
        destination,
      })) as {created: CreatedProject};
      return reply.created;
    } catch (error) {
      throw plain(error, 'Could not copy the project out');
    }
  }

  async #start(host: HostId, inputs: RunInputs): Promise<StartedRun> {
    if ((await this.#deps.transportV2?.(host)) === true) return this.#startRegistered(host, inputs);
    let launch: LiveInstanceRecord | DetachedLaunchFailure;
    try {
      await using lease = await this.#deps.connect(host);
      await lease.connection.ready();
      const fields = runFields(inputs, lease.connection.features ?? []);
      ({launch} = (await lease.connection.request({type: 'run.start', ...fields})) as {
        launch: LiveInstanceRecord | DetachedLaunchFailure;
      });
    } catch (error) {
      throw plain(error, 'Could not start the run');
    }
    if (isFailure(launch)) {
      const log = launch.log_path == null ? '' : ` Its log is ${launch.log_path}.`;
      throw new Error(`${launch.message} (${launch.code} at ${launch.stage}).${log}`);
    }
    await this.#remember(host, inputs, launch.run_id, launch.id);
    return {runId: launch.run_id, instanceId: launch.id};
  }

  async #startRegistered(host: HostId, inputs: RunInputs): Promise<StartedRun> {
    const start = this.#deps.registryStart;
    if (start === undefined) {
      throw new Error('run transport v2 is on and the start registry is missing');
    }
    const controller = new AbortController();
    let started: {runId: string; instanceId: string};
    try {
      started = await start(host, inputs, controller.signal);
    } catch (error) {
      throw plain(error, 'Could not start the run');
    }
    await this.#remember(host, inputs, started.runId, started.instanceId);
    return started;
  }

  async #remember(
    host: HostId,
    inputs: RunInputs,
    runId: string,
    instanceId: string,
  ): Promise<void> {
    await this.#deps.remember({
      host: hostKey(host),
      project: inputs.project,
      task: inputs.task,
      runId,
      instanceId,
      attachedAt: this.#deps.now(),
      start: {
        harness: inputs.harness,
        options: inputs.options,
        agents: inputs.agents,
        measure_on: inputs.measure_on,
        args: inputs.args,
      },
    });
  }
}
