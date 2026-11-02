/**
 * The hosts the window shows when it is opened from a page rather than from the desktop app. Each
 * is a fake daemon serving a generated run through the same ports a real host would, so the
 * screens cannot tell the difference. The real source replaces `portsOf(new FixtureSource(...))`
 * with ports over the host gateway connection, with no screen change.
 */
import type {HostId} from '@vibesys/hosts';
import {FakeDaemon, FixtureSource, generateScenario} from '@vibesys/orchestrator-fixtures';
import {type HostPorts, portsOf, SourceError} from '@vibesys/orchestrator-source';

export interface FixtureHost {
  readonly host: HostId;
  readonly ports: HostPorts;
  /** Commits one more event, as if the run went on; false once everything is committed. */
  readonly advance: () => boolean;
  readonly endMs: number;
}

interface FixtureSpec {
  readonly host: HostId;
  /** The host cannot be reached: every dial fails at once. */
  readonly unreachable?: boolean;
  readonly seed: number;
  readonly subagents: number;
  readonly live: boolean;
  readonly depth: number;
  /** The host accepts a message and later gives up delivering it. */
  readonly rejectsMessages?: boolean;
  /** The connection drops after this many responses on its first connections. */
  readonly dropsAfter: readonly number[];
}

const LONG_RUN: FixtureSpec = {
  host: {kind: 'ssh', alias: 'host-a'},
  seed: 5,
  subagents: 9,
  live: true,
  depth: 30,
  dropsAfter: [],
};
const FINISHED_RUN: FixtureSpec = {
  host: {kind: 'ssh', alias: 'host-b'},
  seed: 8,
  subagents: 4,
  live: false,
  depth: 3,
  dropsAfter: [],
};
const SPECS = {
  'long-run': [LONG_RUN, FINISHED_RUN],
  reconnects: [{...LONG_RUN, dropsAfter: [30, 45, 60]}],
  'short-run': [FINISHED_RUN],
  // The first host answers; the second never does, so the window shows a lost connection.
  disconnected: [LONG_RUN, {...FINISHED_RUN, unreachable: true}],
  // Every message to a live agent is accepted and then rejected, as when the agent's session ends.
  'rejected-message': [{...LONG_RUN, rejectsMessages: true}],
} as const satisfies Record<string, readonly FixtureSpec[]>;

export type FixtureName = keyof typeof SPECS;

export const FIXTURE_NAMES = Object.keys(SPECS) as FixtureName[];

export function fixtureHosts(name: FixtureName, revealed: number | null): FixtureHost[] {
  return SPECS[name].map(spec => {
    const scenario = generateScenario(spec);
    const daemon = new FakeDaemon(scenario, revealed ?? scenario.events.length);
    const base = portsOf(new FixtureSource(daemon, {dropsAfter: spec.dropsAfter}));
    const ports: HostPorts =
      spec.rejectsMessages === true
        ? {
            ...base,
            send: async (command, signal) => {
              const receipt = await base.send(command, signal);
              if (receipt.messageId !== '') daemon.reject(receipt.messageId);
              return receipt;
            },
          }
        : base;
    return {
      host: spec.host,
      ports:
        spec.unreachable === true
          ? {
              ...ports,
              openRuns: () => ({
                frames: {
                  [Symbol.asyncIterator]: () => ({
                    next: () => Promise.reject(new SourceError('lost', 'no route to host')),
                  }),
                },
                close: () => undefined,
              }),
            }
          : ports,
      advance: () => {
        const before = daemon.sequence;
        daemon.commit(1);
        return daemon.sequence > before;
      },
      endMs: scenario.endMs,
    };
  });
}
