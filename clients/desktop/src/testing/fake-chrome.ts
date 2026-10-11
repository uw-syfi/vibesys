/**
 * Random window states (`ChromeState`) for model-based tests of the title strip and the welcome
 * stores, drawn from a `Seeded` generator.
 */
import {type StopView, stopKey} from '../stop-run.js';
import type {ChromeState, RunLink} from '../welcome-protocol.js';
import type {Seeded} from './seeded.js';

const LINKS: readonly RunLink[] = ['connecting', 'connected', 'reconnecting', 'stuck', 'ended'];

export function randomChrome(random: Seeded): ChromeState {
  const attached: ChromeState['attached'] =
    random.next() < 0.2
      ? null
      : {
          host: random.pick(['local', 'ssh:gpu']),
          hostLabel: 'h',
          instanceId: random.pick([null, '0123456789ab', 'ba9876543210']),
          runId: random.pick([null, 'run-1']),
          project: '/p',
          status: 'status',
          detail: '',
          link: random.pick(LINKS),
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
