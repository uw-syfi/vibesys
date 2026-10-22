/**
 * A fake run behind the Fake bridge (`fake-bridge.ts`), for the Run screen's journey: the log
 * streams its instance lists and their text, answered as the main process answers the welcome
 * view's logs requests, and the run commands the page sends, recorded in order on
 * `window.fakeHost.calls`. `installFakeRun` runs in the page after `installFakeBridge`
 * (one ordered `addInitScripts` call), so it is self-contained and adds to that bridge.
 */
import type {FakeProvides} from './fake-bridge.js';

/** What `installFakeRun` provides: the log streams and the run commands. */
export type RunMethods = 'logs' | 'releaseLogs' | 'resume';

export interface FakeRunSeed {
  readonly streams: readonly unknown[];
  /** Each stream's whole text, by stream id. */
  readonly text: Readonly<Record<string, string>>;
}

export function installFakeRun(seed: FakeRunSeed): void {
  type LogsRequest =
    | {kind: 'list'}
    | {kind: 'withdraw'; token: string}
    | {kind: 'read'; token: string; read: {stream_id: string; start: {offset?: number}}};
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const calls: unknown[][] = [];
  const encoder = new TextEncoder();
  /** A read answers what is past its offset, or waits, as the helper's long poll does. */
  const logs = (_host: string, instance: string, request: LogsRequest) => {
    if (request.kind === 'list') return ok({ok: true, instanceId: instance, streams: seed.streams});
    if (request.kind === 'withdraw') return ok(null);
    const text = seed.text[request.read.stream_id] ?? '';
    const size = encoder.encode(text).length;
    const offset = request.read.start.offset ?? 0;
    if (offset >= size) return new Promise(() => {});
    const chunk = {offset, data: text, next_offset: size, eof: true, closed: false};
    return ok({ok: true, chunk: {...chunk, truncated_before: false, pipes: []}});
  };
  const record =
    (name: string) =>
    (...args: unknown[]) => {
      calls.push([name, ...args]);
      return ok(null);
    };
  const target = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(target.vibesysWelcome, {
    logs,
    releaseLogs: () => ok(null),
    resume: record('resume'),
  } satisfies FakeProvides<RunMethods>);
  Object.assign(target.fakeHost, {calls});
}
