/**
 * The daemon's side of `vibesys-orchestrator connect`, in memory: a `ProcessPort` that reads
 * framed requests and answers from a `FixtureSource`. It lets the real `StdioSource` run against a
 * fixture with no process, so the real source and the fixture source are held to one contract.
 */
import {create, type JsonValue} from '@bufbuild/protobuf';
import {
  CommandSchema,
  decodeValidatedJson,
  encodeJson,
  OrchestratorService,
} from '@vibesys/orchestrator-proto';
import {
  type ClientFrame,
  type DaemonFrame,
  encodeFrame,
  FrameDecoder,
  type LaunchProcess,
  type ProcessPort,
} from '@vibesys/orchestrator-source';
import type {FixtureSource} from './fixture-source.js';

type Respond = (response: JsonValue) => void;
type Handler = (request: JsonValue, respond: Respond, signal: AbortSignal) => Promise<void>;

/** One handler per method: read the request, ask the source, answer in the method's output. */
function handlers(source: FixtureSource): Record<string, Handler> {
  const m = OrchestratorService.method;
  return {
    [m.watchRuns.name]: async (request, respond, signal) => {
      const input = decodeValidatedJson(m.watchRuns.input, request);
      for await (const body of source.watchRuns(input.afterSequence, signal)) {
        respond(encodeJson(m.watchRuns.output, create(m.watchRuns.output, {body})));
      }
    },
    [m.watchAgent.name]: async (request, respond, signal) => {
      const input = decodeValidatedJson(m.watchAgent.input, request);
      for await (const body of source.watchAgent(
        input.agentId,
        input.pageSize,
        input.afterSequence,
        signal,
      )) {
        respond(encodeJson(m.watchAgent.output, create(m.watchAgent.output, {body})));
      }
    },
    [m.listItems.name]: async (request, respond, signal) => {
      const input = decodeValidatedJson(m.listItems.input, request);
      const page = await source.listItems(
        input.agentId,
        input.beforeOrdinal,
        input.pageSize,
        signal,
      );
      respond(encodeJson(m.listItems.output, create(m.listItems.output, {page})));
    },
    [m.listPlans.name]: async (request, respond, signal) => {
      const input = decodeValidatedJson(m.listPlans.input, request);
      const plans = [...(await source.listPlans(input.runId, signal))];
      respond(encodeJson(m.listPlans.output, create(m.listPlans.output, {plans})));
    },
    [m.dispatch.name]: async (request, respond, signal) => {
      const input = decodeValidatedJson(m.dispatch.input, request);
      const receipt = await source.dispatch(input.command ?? create(CommandSchema), signal);
      respond(encodeJson(m.dispatch.output, create(m.dispatch.output, {receipt})));
    },
  };
}

export function loopbackLauncher(source: FixtureSource): LaunchProcess {
  const table = handlers(source);
  return (): ProcessPort => {
    const decoder = new FrameDecoder();
    const calls = new Map<number, AbortController>();
    const listeners = {
      data: (_chunk: Uint8Array) => {},
      close: (_detail: string) => {},
    };
    let dead = false;
    const send = (frame: DaemonFrame): void => {
      if (!dead) queueMicrotask(() => listeners.data(encodeFrame(frame)));
    };

    const run = async (frame: Extract<ClientFrame, {method: string}>, signal: AbortSignal) => {
      const handler = table[frame.method];
      try {
        if (handler === undefined) throw new Error(`unknown method ${frame.method}`);
        await handler(
          frame.request as JsonValue,
          response => send({id: frame.id, response}),
          signal,
        );
        send({id: frame.id, end: true});
      } catch (error) {
        const lost = error instanceof Error && error.name === 'SourceError';
        send({id: frame.id, error: {kind: lost ? 'lost' : 'refused', message: String(error)}});
      }
    };

    return {
      write(bytes) {
        for (const frame of decoder.push(bytes) as ClientFrame[]) {
          if ('cancel' in frame) {
            calls.get(frame.id)?.abort();
            calls.delete(frame.id);
            continue;
          }
          const controller = new AbortController();
          calls.set(frame.id, controller);
          void run(frame, controller.signal);
        }
      },
      onData(listener) {
        listeners.data = listener;
      },
      onClose(listener) {
        listeners.close = listener;
      },
      kill() {
        if (dead) return;
        dead = true;
        for (const call of calls.values()) call.abort();
        listeners.close('killed');
      },
    };
  };
}
