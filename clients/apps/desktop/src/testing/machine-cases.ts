/**
 * The worlds the Machines page is looked at in, each a set of hosts with what their links, machine
 * stats and eval places say: `one` (this machine, live, with its readings), `several` (live,
 * reconnecting, refused, a very long alias, places reachable and not) and `unsupported` (a machine
 * whose host cannot sample stats). A world wraps the fixture's recording: every call but the stats
 * stream, the eval places and the link keeps the answer the recording gives it.
 */
import {create, toBinary} from '@bufbuild/protobuf';
import {
  AcceleratorModel,
  AcceleratorVendor,
  type EvalPlace,
  EvalPlaceKind,
  EvalPlaceSchema,
  ListEvalPlacesResponseSchema,
  OffersUnreadableReason,
} from '@vibesys/orchestrator-proto';
import {AbsenceReason, DesktopRefusal, TopicFailureCode} from '@vibesys/proto';
import {RpcStatus} from '../core/errors.js';
import {
  type MachineFigures,
  refusedFrame,
  SUBSCRIBE,
  SYNCED_FRAME,
  statsFrame,
  TWO_GPUS,
} from './machine-stats.js';
import {offer, place} from './orchestrator/new-run-fixtures.js';
import {methodPathOf} from './orchestrator/recordings.js';
import type {Recording, Step} from './transport/index.js';

export const MACHINE_CASES = ['one', 'several', 'unsupported'] as const;
export type MachineCaseName = (typeof MACHINE_CASES)[number];

/** A host of a world: its key and label as the window names it. */
export interface MachineCaseHost {
  readonly key: string;
  readonly label: string;
}

/** What one host says of itself. */
interface HostWorld extends MachineCaseHost {
  /** `live`, `lost` (its link drops once its stats stream has sent a sample) or `refused` (the app has no host program for it). */
  readonly link: 'live' | 'lost' | 'refused';
  /** Its stats stream, or `unsupported` for a host that refuses the topic; `none` leaves the call to the recording. */
  readonly stats: MachineFigures | 'unsupported' | 'none';
  readonly places: readonly EvalPlace[];
}

const GIB = 1024n ** 3n;

const unreachable = (name: string, kind: EvalPlaceKind, detail: string): EvalPlace =>
  create(EvalPlaceSchema, {
    name,
    kind,
    state: {case: 'unreadable', value: {reason: OffersUnreadableReason.UNREACHABLE, detail}},
  });

const h100 = offer(AcceleratorModel.H100, AcceleratorVendor.NVIDIA, 8, 'NVIDIA H100 80GB HBM3');
const mi300 = offer(AcceleratorModel.MI300X, AcceleratorVendor.AMD, 8, 'AMD Instinct MI300X');

const eightGpus: MachineFigures = {
  cpuPercent: 74.2,
  memory: {usedBytes: 1840n * GIB, totalBytes: 2048n * GIB},
  gpus: Array.from({length: 8}, (_, index) => ({
    name: 'NVIDIA H100 80GB HBM3',
    utilizationPercent: 60 + index * 5,
    memoryUsedBytes: BigInt(40 + index * 4) * GIB,
    memoryTotalBytes: 80n * GIB,
  })),
};

const cpuOnly: MachineFigures = {
  cpuPercent: 8.4,
  memory: {usedBytes: 12n * GIB, totalBytes: 64n * GIB},
  gpus: [],
};

const noGpuTool: MachineFigures = {
  cpuPercent: 21,
  memory: {usedBytes: 20n * GIB, totalBytes: 32n * GIB},
  gpus: {absent: AbsenceReason.TOOL_NOT_FOUND},
};

const LONG_ALIAS = 'gpu-training-cluster-login-node-01.region-east.research.example.internal';

const WORLDS: Readonly<Record<MachineCaseName, readonly HostWorld[]>> = {
  one: [
    {
      key: 'local',
      label: 'This machine',
      link: 'live',
      stats: TWO_GPUS,
      places: [
        place('local', EvalPlaceKind.LOCAL, []),
        place('host-a', EvalPlaceKind.SSH, [h100]),
        place('cluster-a', EvalPlaceKind.SLURM, [mi300]),
        unreachable('cluster-b', EvalPlaceKind.SLURM, 'connection timed out after 30 s'),
      ],
    },
  ],
  several: [
    {
      key: 'local',
      label: 'This machine',
      link: 'live',
      stats: TWO_GPUS,
      places: [
        place('local', EvalPlaceKind.LOCAL, []),
        place('host-a', EvalPlaceKind.SSH, [h100]),
        unreachable('cluster-b', EvalPlaceKind.SLURM, 'connection timed out after 30 s'),
      ],
    },
    {
      key: 'ssh:host-a',
      label: 'host-a',
      link: 'live',
      stats: eightGpus,
      places: [
        place('host-a', EvalPlaceKind.SSH, [h100]),
        place('cluster-a', EvalPlaceKind.SLURM, [mi300]),
      ],
    },
    {
      key: 'ssh:host-b',
      label: 'host-b',
      link: 'lost',
      stats: cpuOnly,
      places: [place('local', EvalPlaceKind.LOCAL, [])],
    },
    {key: 'ssh:host-c', label: 'host-c', link: 'refused', stats: 'none', places: []},
    {
      key: `ssh:${LONG_ALIAS}`,
      label: LONG_ALIAS,
      link: 'live',
      stats: noGpuTool,
      places: [
        place('local', EvalPlaceKind.LOCAL, []),
        place('queue-a', EvalPlaceKind.SLURM, [mi300]),
        place('queue-b', EvalPlaceKind.SLURM, [h100]),
        place('queue-c', EvalPlaceKind.APPTAINER, [h100]),
        unreachable('queue-d', EvalPlaceKind.SLURM, 'no route to host'),
        unreachable('queue-e', EvalPlaceKind.SLURM, 'connection refused'),
      ],
    },
  ],
  unsupported: [
    {
      key: 'local',
      label: 'This machine',
      link: 'live',
      stats: 'unsupported',
      places: [place('local', EvalPlaceKind.LOCAL, []), place('host-a', EvalPlaceKind.SSH, [h100])],
    },
  ],
};

/** The hosts of a world, in the order the page lists them. */
export const machineCaseHosts = (name: MachineCaseName): readonly MachineCaseHost[] =>
  WORLDS[name].map(({key, label}) => ({key, label}));

/** The host program is not there for the host's platform, which no retry changes. */
const REFUSED: Step = {
  end: {refusal: DesktopRefusal.HOST_PROGRAM_MISSING, message: 'no host program for this platform'},
};

const LINK_LOST: Step = {end: {message: 'link lost', status: RpcStatus.UNAVAILABLE}};

/**
 * The stats stream of a host: its first sample and the snapshot's end, then it stays open, or (for a
 * host whose link drops) the link is lost right after, and each stream after it fails at once.
 */
const statsSteps = (
  stats: HostWorld['stats'],
  link: HostWorld['link'],
  own: number,
): readonly Step[] | null => {
  if (stats === 'none') return null;
  if (stats === 'unsupported') return [refusedFrame(TopicFailureCode.UNSUPPORTED)];
  if (link === 'lost')
    return own === 0 ? [statsFrame(stats), SYNCED_FRAME, LINK_LOST] : [LINK_LOST];
  return [statsFrame(stats), SYNCED_FRAME, {hang: true}];
};

/** `base` with each host of the world saying what the world says of it. */
export function machineCaseRecording(name: MachineCaseName, base: Recording): Recording {
  const hosts = new Map(WORLDS[name].map(host => [host.key, host]));
  // The recording counts a method's calls across hosts; a host's own count says which call is its first.
  const calls = new Map<string, number>();
  return {
    respond: (target, nth) => {
      const host = hosts.get(target.host);
      if (host === undefined) return base.respond(target, nth);
      const id = `${target.host}|${target.method}`;
      const own = calls.get(id) ?? 0;
      calls.set(id, own + 1);
      if (host.link === 'refused') {
        return [REFUSED];
      }
      if (target.method === SUBSCRIBE) {
        return statsSteps(host.stats, host.link, own) ?? base.respond(target, nth);
      }
      if (target.method === methodPathOf('ListEvalPlaces')) {
        return [
          {
            frame: toBinary(
              ListEvalPlacesResponseSchema,
              create(ListEvalPlacesResponseSchema, {places: [...host.places]}),
            ),
          },
        ];
      }
      // A host whose link drops is lost from its second dial on: the first is the one that worked.
      if (host.link === 'lost' && target.method === methodPathOf('Hello') && own > 0) {
        return [LINK_LOST];
      }
      return base.respond(target, nth);
    },
  };
}
