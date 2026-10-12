/**
 * The argument parsers every welcome request shares. Each takes a value the view sent and returns
 * it typed, or throws an `Error` whose message names what is wrong, so a failed request tells the
 * view why.
 */
import {validHostPath} from '@vibesys/hosts';
import {isInstanceId} from '@vibesys/runs';

/** The channel names of one group of requests, as `{method: channel}`. */
export type Channels = Readonly<Record<string, string>>;

/** One channel of `channels`. */
export type ChannelOf<C extends Channels> = C[keyof C];

/** Whether `channel` is one of `channels`. */
export function isChannelOf<C extends Channels>(
  channels: C,
  channel: string,
): channel is ChannelOf<C> {
  return Object.values(channels).includes(channel);
}

/** A live run's registry id, to `verb` (the view's word for what it was asked to do). */
export function instance(value: unknown, verb: string): string {
  if (!isInstanceId(value)) throw new Error(`pick a run to ${verb}`);
  return value;
}

/** A project directory on a host. */
export function project(value: unknown): string {
  return validHostPath(value, 'the project directory');
}

/** A token the view chose to name one operation (a start, a checkout change): a positive integer. */
export function operationToken(value: unknown, operation: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
    const article = /^[aeiou]/i.test(operation) ? 'an' : 'a';
    throw new Error(`${article} ${operation} is named by a positive integer`);
  }
  return value;
}
