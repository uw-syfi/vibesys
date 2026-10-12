/**
 * Random window states (`ChromeState`) for model-based tests of the stores that fold them (the
 * title strip, the host panel, the run controls), drawn from a `Seeded` generator.
 */

import type {ConnectionStatus} from '@vibesys/hosts';
import type {ChromeState} from '@vibesys/ipc';
import {type StopView, stopKey} from '@vibesys/runs';
import type {Seeded} from '@vibesys/store/testing';

const CONNECTIONS: readonly ConnectionStatus[] = [
  {kind: 'connecting'},
  {kind: 'connected'},
  {kind: 'reconnecting', attempt: 2},
  {kind: 'offline', detail: 'no network'},
  {kind: 'auth-needed', detail: 'password'},
  {kind: 'run-ended'},
  {kind: 'incompatible', detail: 'protocol 3 vs 4'},
  {kind: 'closed'},
];

export function randomChrome(random: Seeded): ChromeState {
  const attached: ChromeState['attached'] =
    random.next() < 0.2
      ? null
      : {
          host: random.pick(['local', 'ssh:gpu']),
          hostLabel: 'h',
          instanceId: random.pick(['0123456789ab', 'ba9876543210']),
          runId: random.pick(['run-1', 'run-2']),
          project: '/p',
          connection: random.pick(CONNECTIONS),
        };
  const stops: Record<string, StopView> = {};
  if (attached?.instanceId != null && random.next() < 0.6) {
    const phase = random.pick(['idle', 'stopping', 'ended', 'error'] as const);
    stops[stopKey(attached.host, attached.instanceId)] = {
      phase,
      text: phase,
      canForce: phase === 'error' && random.bool(),
    };
  }
  return {mode: random.pick(['welcome', 'run'] as const), attached, stops};
}
