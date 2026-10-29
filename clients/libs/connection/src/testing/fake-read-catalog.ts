/** An in-memory registry read peer; it validates the same wire requests as the gateway. */
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {
  Accelerator,
  type AgentLoginSchema,
  API_SCHEMA_HASH,
  type CandidateIdentitySchema,
  CandidateState,
  type ExampleListSchema,
  type HarnessOptionSchema,
  type InspectCandidateRequest,
  MachineDescriptionSchema,
  type PreflightRunRequest,
  PreflightRunResponseSchema,
  type ReadRequest,
  ReadRequestSchema,
  ReadResponseSchema,
  type TaskGoalSchema,
  validatedFromBinary,
} from '@vibesys/proto';
import type {RegistryReadPorts} from '../shell/registry-reads.js';

export class FakeReadCatalog implements RegistryReadPorts {
  #inspectionGate: {path: string; arrived: () => void; released: Promise<void>} | undefined;

  deferInspection(path: string): {received: Promise<void>; release: () => void} {
    if (this.#inspectionGate) throw new Error('inspection already held');
    let arrived = () => {};
    let release = () => {};
    const received = new Promise<void>(resolve => {
      arrived = resolve;
    });
    const released = new Promise<void>(resolve => {
      release = resolve;
    });
    this.#inspectionGate = {path, arrived, released};
    return {received, release};
  }

  savedCheckout: string | null = '/checkout';
  readonly requests: ReadRequest[] = [];
  readonly candidates = new Map<string, MessageInitShape<typeof CandidateIdentitySchema>>();
  readonly projects = new Map<string, string[]>();
  readonly goals = new Map<string, MessageInitShape<typeof TaskGoalSchema>>();
  loginFacts: MessageInitShape<typeof AgentLoginSchema>[] = [];
  harnessOptions: MessageInitShape<typeof HarnessOptionSchema>[] = [];
  exampleList: MessageInitShape<typeof ExampleListSchema> = {version: 1n, examples: []};
  machine = create(MachineDescriptionSchema, {
    hardware: {cpuCores: 8n, accelerator: Accelerator.CPU},
    docker: {
      value: {case: 'dockerUnreachable', value: {reachable: false, reason: 'not installed'}},
    },
    runs: {live: 0n, endedToday: 0n},
  });
  preflightResult = create(PreflightRunResponseSchema);
  failure: Error | undefined;

  async checkout(): Promise<string | null> {
    return this.savedCheckout;
  }

  async unary(_host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array> {
    if (signal.aborted) throw new Error('operation cancelled');
    if (this.failure !== undefined) throw this.failure;
    const request = validatedFromBinary(ReadRequestSchema, bytes);
    if (request.method.case !== 'inspectCandidate' && request.checkout !== this.savedCheckout)
      throw new Error('checkout changed');
    this.requests.push(request);
    const method = request.method;
    switch (method.case) {
      case 'inspectCandidate':
        return this.inspect(method.value, signal);

      case 'listTasks': {
        const tasks = this.projects.get(method.value.project);
        if (tasks === undefined) throw new Error('unknown project');
        return this.reply({
          result: {
            case: 'listTasks',
            value: {
              requestId: method.value.requestId,
              tasks: {
                version: 1n,
                projectRoot: method.value.project,
                tasks: tasks.map(name => ({name})),
              },
            },
          },
        });
      }
      case 'listAgentLogins':
        return this.reply({
          result: {
            case: 'listAgentLogins',
            value: {requestId: method.value.requestId, logins: this.loginFacts},
          },
        });
      case 'listHarnesses':
        return this.reply({
          result: {
            case: 'listHarnesses',
            value: {requestId: method.value.requestId, harnesses: this.harnessOptions},
          },
        });
      case 'listExamples':
        return this.reply({
          result: {
            case: 'listExamples',
            value: {requestId: method.value.requestId, listing: this.exampleList},
          },
        });
      case 'getTaskGoal': {
        const goal = this.goals.get(`${method.value.project}/${method.value.task}`);
        if (goal === undefined) throw new Error('unknown task');
        return this.reply({
          result: {case: 'getTaskGoal', value: {requestId: method.value.requestId, goal}},
        });
      }
      case 'describeMachine':
        return this.reply({
          result: {
            case: 'describeMachine',
            value: {requestId: method.value.requestId, machine: this.machine},
          },
        });
      case 'preflightRun':
        return this.preflight(method.value);
      default:
        throw new Error(`unsupported read: ${method.case}`);
    }
  }

  private async inspect(
    request: InspectCandidateRequest,
    signal: AbortSignal,
  ): Promise<Uint8Array> {
    const gate = this.#inspectionGate;
    if (gate?.path === request.path) {
      this.#inspectionGate = undefined;
      gate.arrived();
      await gate.released;
      if (signal.aborted) throw new Error('operation cancelled');
    }
    const identity = this.candidates.get(request.path);
    return this.reply({
      result: {
        case: 'inspectCandidate',
        value: {
          requestId: request.requestId,
          selectionRevision: request.selectionRevision,
          schemaHash: API_SCHEMA_HASH,
          minimumVersion: '0.1.1',
          state: identity === undefined ? CandidateState.NOT_FOUND : CandidateState.FOUND,
          ...(identity === undefined ? {} : {identity}),
        },
      },
    });
  }

  private preflight(request: PreflightRunRequest): Uint8Array {
    return this.reply({
      result: {
        case: 'preflightRun',
        value: {
          requestId: request.requestId,
          checks: this.preflightResult.checks,
          roles: request.withRoles === true ? this.preflightResult.roles : undefined,
          backend: request.withBackend === true ? this.preflightResult.backend : undefined,
        },
      },
    });
  }

  private reply(value: MessageInitShape<typeof ReadResponseSchema>): Uint8Array {
    const bytes = toBinary(ReadResponseSchema, create(ReadResponseSchema, value));
    validatedFromBinary(ReadResponseSchema, bytes);
    return bytes;
  }
}
