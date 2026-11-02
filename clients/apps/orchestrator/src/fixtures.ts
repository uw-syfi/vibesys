/**
 * The hosts the window shows when it is opened from a page rather than over ssh. Each is a fake
 * daemon serving a generated run through the same ports a remote daemon would, so the screens
 * cannot tell the difference. The real source plugs in at `connectionsFor`: a host name becomes
 * `portsOf(connectOverSsh(host))` from `@vibesys/feature-orchestrator/main`, with no screen change.
 */
import {FakeDaemon, FixtureSource, generateScenario} from '@vibesys/orchestrator-fixtures';
import {type HostPorts, portsOf} from '@vibesys/orchestrator-source';

export interface FixtureHost {
  readonly host: string;
  readonly ports: HostPorts;
  /** Commits one more event, as if the run went on; false once everything is committed. */
  readonly advance: () => boolean;
  readonly endMs: number;
}

interface FixtureSpec {
  readonly host: string;
  readonly seed: number;
  readonly subagents: number;
  readonly live: boolean;
  readonly depth: number;
  /** The connection drops after this many responses on its first connections. */
  readonly dropsAfter: readonly number[];
}

const LONG_RUN: FixtureSpec = {
  host: 'host-a',
  seed: 5,
  subagents: 9,
  live: true,
  depth: 30,
  dropsAfter: [],
};
const FINISHED_RUN: FixtureSpec = {
  host: 'host-b',
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
} as const satisfies Record<string, readonly FixtureSpec[]>;

export type FixtureName = keyof typeof SPECS;

export const FIXTURE_NAMES = Object.keys(SPECS) as FixtureName[];

export function fixtureHosts(name: FixtureName, revealed: number | null): FixtureHost[] {
  return SPECS[name].map(spec => {
    const scenario = generateScenario(spec);
    const daemon = new FakeDaemon(scenario, revealed ?? scenario.events.length);
    return {
      host: spec.host,
      ports: portsOf(new FixtureSource(daemon, {dropsAfter: spec.dropsAfter})),
      advance: () => {
        const before = daemon.sequence;
        daemon.commit(1);
        return daemon.sequence > before;
      },
      endMs: scenario.endMs,
    };
  });
}
