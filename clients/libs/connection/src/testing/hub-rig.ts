/** The subscription hub over fake time and a scripted gateway, for the tests that drive it. */
import {FakeClock} from '@vibesys/transport/testing';
import {Subscriptions} from '../shell/subscriptions.js';
import {FakeSubscribeStreams} from './fake-subscribe-streams.js';

export function hubRig() {
  const clock = new FakeClock();
  const gateway = new FakeSubscribeStreams();
  const reported: unknown[] = [];
  const subscriptions = new Subscriptions(
    gateway,
    clock.schedule,
    () => clock.now(),
    error => reported.push(error),
  );
  return {clock, gateway, subscriptions, reported};
}

/** Let the outcome of a stream that ended reach its machine. */
export async function settle(): Promise<void> {
  for (let drain = 0; drain < 5; drain += 1) await Promise.resolve();
}
