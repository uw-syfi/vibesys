/**
 * A gateway's front for an `OrchestratorSource`: what `vibesysd` does with `OrchestratorService`,
 * in memory. It decodes each request, asks the source behind it, and answers with encoded
 * messages and the gRPC status a failure would carry. Over a `FixtureSource` it lets the real
 * source (`GatewaySource`) run the same contract suite as the fixture source, with no socket.
 */
import {create} from '@bufbuild/protobuf';
import {
  BuildVersionsSchema,
  Compatibility,
  DispatchRequestSchema,
  DispatchResponseSchema,
  decodeValidated,
  encodeMessage,
  HelloResponseSchema,
  ItemsPageSchema,
  ListItemsRequestSchema,
  ListItemsResponseSchema,
  ListPlansRequestSchema,
  ListPlansResponseSchema,
  OrchestratorService,
  WatchAgentRequestSchema,
  WatchAgentResponseSchema,
  WatchRunsRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import {
  type GatewayCaller,
  type OrchestratorSource,
  SourceError,
} from '@vibesys/orchestrator-source';

/** How a call can fail at the gateway: the status the daemon answers with, and why. */
export interface Refusal {
  readonly status: number;
  readonly message: string;
}

export interface FakeGatewayOptions {
  /** Every call fails like this, as a second `vibesysd` answers UNAVAILABLE with the holder named. */
  readonly refuse?: Refusal;
  /** What `Hello` answers; compatible unless set. */
  readonly compatibility?: Compatibility;
}

/** A failed call as the bridge reports it: the text, and the gRPC status when there was one. */
class FakeGatewayError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const UNAVAILABLE = 14;
const UNIMPLEMENTED = 12;

/** A source that fails is a connection that dropped, which the gateway reports as UNAVAILABLE. */
function statusOf(error: unknown): number | undefined {
  if (error instanceof FakeGatewayError) return error.status;
  return error instanceof SourceError ? UNAVAILABLE : undefined;
}

export class FakeGateway implements GatewayCaller {
  constructor(
    private readonly source: OrchestratorSource,
    private readonly options: FakeGatewayOptions = {},
  ) {}

  #refused(): FakeGatewayError | null {
    const {refuse} = this.options;
    return refuse === undefined ? null : new FakeGatewayError(refuse.message, refuse.status);
  }

  #name(method: string): string {
    const prefix = `/${OrchestratorService.typeName}/`;
    if (!method.startsWith(prefix))
      throw new FakeGatewayError(`unknown method ${method}`, UNIMPLEMENTED);
    return method.slice(prefix.length);
  }

  unary(_host: string, method: string, bytes: Uint8Array) {
    const abort = new AbortController();
    const answer = async (): Promise<Uint8Array> => {
      const refused = this.#refused();
      if (refused !== null) throw refused;
      const {signal} = abort;
      switch (this.#name(method)) {
        case 'Hello':
          return encodeMessage(
            HelloResponseSchema,
            create(HelloResponseSchema, {
              daemon: create(BuildVersionsSchema, {
                binaryVersion: 'fake',
                protocolVersion: 1,
                minProtocolVersion: 1,
              }),
              compatibility: this.options.compatibility ?? Compatibility.COMPATIBLE,
            }),
          );
        case 'Dispatch': {
          const {command} = decodeValidated(DispatchRequestSchema, bytes);
          if (command === undefined) throw new FakeGatewayError('no command', 3);
          const receipt = await this.source.dispatch(command, signal);
          return encodeMessage(DispatchResponseSchema, create(DispatchResponseSchema, {receipt}));
        }
        case 'ListItems': {
          const request = decodeValidated(ListItemsRequestSchema, bytes);
          const page = await this.source.listItems(
            request.agentId,
            request.beforeOrdinal,
            request.pageSize,
            signal,
          );
          return encodeMessage(
            ListItemsResponseSchema,
            create(ListItemsResponseSchema, {page: create(ItemsPageSchema, page)}),
          );
        }
        case 'ListPlans': {
          const {runId} = decodeValidated(ListPlansRequestSchema, bytes);
          const plans = await this.source.listPlans(runId, signal);
          return encodeMessage(
            ListPlansResponseSchema,
            create(ListPlansResponseSchema, {plans: [...plans]}),
          );
        }
        default:
          throw new FakeGatewayError(`unknown method ${method}`, UNIMPLEMENTED);
      }
    };
    return {result: answer(), cancel: () => abort.abort()};
  }

  async *#frames(
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
  ): AsyncGenerator<Uint8Array> {
    const refused = this.#refused();
    if (refused !== null) throw refused;
    switch (this.#name(method)) {
      case 'WatchRuns': {
        const {afterSequence} = decodeValidated(WatchRunsRequestSchema, bytes);
        for await (const body of this.source.watchRuns(afterSequence, signal))
          yield encodeMessage(WatchRunsResponseSchema, create(WatchRunsResponseSchema, {body}));
        return;
      }
      case 'WatchAgent': {
        const request = decodeValidated(WatchAgentRequestSchema, bytes);
        for await (const response of this.source.watchAgent(
          request.agentId,
          request.pageSize,
          request.afterSequence,
          signal,
        ))
          yield encodeMessage(WatchAgentResponseSchema, response);
        return;
      }
      default:
        throw new FakeGatewayError(`unknown method ${method}`, UNIMPLEMENTED);
    }
  }

  stream(
    _host: string,
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string, status?: number) => void,
  ) {
    const abort = new AbortController();
    let done = false;
    const finish = (error?: string, status?: number): void => {
      if (done) return;
      done = true;
      ended(error, status);
    };
    void (async () => {
      try {
        for await (const frame of this.#frames(method, bytes, abort.signal)) {
          if (done) return;
          receive(frame);
        }
        finish();
      } catch (error) {
        finish(error instanceof Error ? error.message : String(error), statusOf(error));
      }
    })();
    return {
      cancel: () => {
        abort.abort();
        finish('cancelled');
      },
    };
  }
}
