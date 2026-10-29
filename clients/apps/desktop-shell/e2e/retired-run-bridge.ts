/** Fail a journey immediately if the native page accidentally calls a retired run reader. */
import type {FakeProvides} from './fake-bridge.js';

export type RetiredRunMethods =
  | 'attach'
  | 'attachReader'
  | 'releaseRun'
  | 'connectRun'
  | 'onWake'
  | 'readEvents'
  | 'readTrace'
  | 'readSummary'
  | 'readSpan'
  | 'logs'
  | 'releaseLogs'
  | 'resume';

export function installGatewayOnly(): void {
  const target = window as unknown as {vibesysWelcome: object; fakeHost: object};
  const retired = (method: string) => () => {
    throw new Error(`Retired run bridge call: ${method}`);
  };
  Object.assign(target.vibesysWelcome, {
    attach: retired('attach'),
    attachReader: retired('attachReader'),
    releaseRun: retired('releaseRun'),
    connectRun: retired('connectRun'),
    onWake: retired('onWake'),
    readEvents: retired('readEvents'),
    readTrace: retired('readTrace'),
    readSummary: retired('readSummary'),
    readSpan: retired('readSpan'),
    logs: retired('logs'),
    releaseLogs: retired('releaseLogs'),
    resume: retired('resume'),
  } satisfies FakeProvides<RetiredRunMethods>);
  Object.assign(target.fakeHost, {commands: [], calls: [], readerAttaches: () => 0});
}
