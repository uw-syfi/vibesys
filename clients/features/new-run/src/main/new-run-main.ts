/**
 * The New run feature in the main process: it answers the screen's requests with one host helper
 * method each, over the host's connection, and records a started run in Recent.
 */
import {
  ConnectionError,
  isHostUnsupported,
  type RegistryCatalog,
  type RegistryProjects,
  type RegistryRunCatalog,
  type RunWatch,
} from '@vibesys/connection';
import type {
  CreatedProject,
  ExampleList,
  HelperFeature,
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
import {NATIVE_FEATURES} from './native-features.js';

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
  /** Record a started run in Recent (the settings store). */
  readonly remember: (run: StoredRecentRun) => Promise<void>;
  readonly now: () => number;
  /**
   * The registry start used when using the gateway. The host keeps the
   * admitted run after `signal` aborts.
   */
  readonly registryStart?: (
    host: HostId,
    inputs: RunInputs,
    signal: AbortSignal,
  ) => Promise<{readonly runId: string; readonly instanceId: string}>;
  /** Create, copy, and destination checks when using the gateway. */
  readonly registryProjects?: RegistryProjects;
  readonly registryCatalog?: RegistryRunCatalog;
  readonly registryReads?: RegistryCatalog;
  /** WatchRuns for the host a start just launched on. */
  readonly watchRuns?: Pick<RunWatch, 'start'>;
}

/** A failure the user reads: the helper's message, prefixed with what was being done. */
function plain(error: unknown, doing: string): Error {
  const detail = error instanceof Error ? error.message : String(error);
  // A connection failure keeps its typed cause, which the screen words apart from the raw text.
  if (error instanceof ConnectionError) return error.reworded(`${doing}: ${detail}`);
  return new Error(`${doing}: ${detail}`);
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
        return this.#describe(request.host);
      case 'preflight':
        return this.#preflight(request.host, request.inputs);
      case 'examples':
        return this.#examples(request.host);
      case 'create-from-example':
        return this.#createFromExample(request.host, request.example, request.destination);
      case 'example-destination':
        return this.#destination(request.host, request.example, request.destination);
      case 'copy-out':
        return this.#copyOut(request.host, request.source, request.destination);
      case 'start-run':
        return this.#start(request.host, request.inputs);
      case 'helper-features':
        return this.#features();
      case 'task-goal':
        return this.#taskGoal(request.host, request.project, request.task);
    }
  }

  async #taskGoal(host: HostId, project: string, task: string): Promise<Offered<TaskGoal>> {
    try {
      return {
        kind: 'offered',
        value: await this.#catalog().taskGoal(host, project, task, new AbortController().signal),
      };
    } catch (error) {
      throw plain(error, `Could not read the goal of ${task}`);
    }
  }

  async #describe(host: HostId): Promise<Offered<MachineDescription>> {
    const reads = this.#deps.registryReads;
    if (reads === undefined) throw new Error('machine registry is missing');
    try {
      return {
        kind: 'offered',
        value: await reads.describeMachine(host, null, new AbortController().signal),
      };
    } catch (error) {
      throw plain(error, 'Could not describe the machine');
    }
  }

  #catalog(): RegistryRunCatalog {
    const catalog = this.#deps.registryCatalog;
    if (catalog === undefined) throw new Error('new-run registry is missing');
    return catalog;
  }

  /** What the host offers: the gateway's whole registry; an older host refuses call by call. */
  async #features(): Promise<readonly HelperFeature[]> {
    return NATIVE_FEATURES;
  }

  /**
   * The checks for `inputs`, with the run's roles when the helper agreed to `run_roles`, and its
   * backend (and each failure's typed cause) when it agreed to `run_backend`, and the typed cause
   * of every other failed setting when it agreed to `run_causes`.
   */
  async #preflight(host: HostId, inputs: RunInputs): Promise<Offered<PreflightResult>> {
    try {
      return {
        kind: 'offered',
        value: await this.#catalog().preflight(host, inputs, new AbortController().signal),
      };
    } catch (error) {
      if (isHostUnsupported(error)) {
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
      return {
        kind: 'offered',
        value: await this.#catalog().examples(host, new AbortController().signal),
      };
    } catch (error) {
      if (isHostUnsupported(error)) {
        return {kind: 'unsupported'};
      }
      throw plain(error, `Could not list the examples on ${hostLabel(host)}`);
    }
  }

  async #destination(
    host: HostId,
    example: string,
    destination: string,
  ): Promise<Offered<DestinationFound>> {
    try {
      const found = await this.#projects().destination(
        host,
        example,
        destination,
        new AbortController().signal,
      );
      return {kind: 'offered', value: found};
    } catch (error) {
      if (isHostUnsupported(error)) return {kind: 'unsupported'};
      throw plain(error, `Could not check the destination on ${hostLabel(host)}`);
    }
  }

  /** The new project, or `taken` when the destination holds something (it is not for the user to read). */
  async #createFromExample(
    host: HostId,
    example: string,
    destination: string,
  ): Promise<ExampleCreation> {
    try {
      return await this.#projects().create(
        host,
        example,
        destination,
        new AbortController().signal,
      );
    } catch (error) {
      throw plain(error, 'Could not create the project');
    }
  }

  async #copyOut(host: HostId, source: string, destination: string): Promise<CreatedProject> {
    try {
      return await this.#projects().copy(host, source, destination, new AbortController().signal);
    } catch (error) {
      throw plain(error, 'Could not copy the project out');
    }
  }

  async #start(host: HostId, inputs: RunInputs): Promise<StartedRun> {
    return this.#startRegistered(host, inputs);
  }

  #projects(): RegistryProjects {
    const projects = this.#deps.registryProjects;
    if (projects === undefined) {
      throw new Error('the project registry is missing');
    }
    return projects;
  }

  async #startRegistered(host: HostId, inputs: RunInputs): Promise<StartedRun> {
    const start = this.#deps.registryStart;
    if (start === undefined) {
      throw new Error('the start registry is missing');
    }
    const controller = new AbortController();
    let started: {runId: string; instanceId: string};
    try {
      started = await start(host, inputs, controller.signal);
    } catch (error) {
      throw plain(error, 'Could not start the run');
    }
    const watch = this.#deps.watchRuns;
    if (watch === undefined) {
      throw new Error('the run watch is missing');
    }
    watch.start(host);
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
