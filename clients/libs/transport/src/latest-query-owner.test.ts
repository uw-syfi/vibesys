import {describe, expect, test} from 'bun:test';
import {LatestQueryOwner} from './index.js';

interface Deferred<T> {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return {promise, resolve, reject};
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe('LatestQueryOwner', () => {
  test('suppresses a superseded success and settles every waiter on the follow-up', async () => {
    const predecessor = deferred<string>();
    const followUp = deferred<string>();
    const requests = [predecessor.promise, followUp.promise];
    const published: string[] = [];
    const owner = new LatestQueryOwner({
      request: () => {
        const request = requests.shift();
        if (request === undefined) throw new Error('unexpected query');
        return request;
      },
      publish: value => published.push(value),
      report: error => {
        throw error;
      },
    });
    let leaderSettled = false;
    let followerSettled = false;
    const leader = owner.refresh().then(() => {
      leaderSettled = true;
    });
    const follower = owner.refresh().then(() => {
      followerSettled = true;
    });

    predecessor.resolve('stale');
    await settle();
    expect(published).toEqual([]);
    expect(leaderSettled).toBe(false);
    expect(followerSettled).toBe(false);

    followUp.resolve('current');
    await Promise.all([leader, follower]);
    expect(published).toEqual(['current']);
  });

  test('suppresses a superseded failure when the follow-up succeeds', async () => {
    const predecessor = deferred<string>();
    const followUp = deferred<string>();
    const requests = [predecessor.promise, followUp.promise];
    const published: string[] = [];
    const reported: unknown[] = [];
    const owner = new LatestQueryOwner({
      request: () => {
        const request = requests.shift();
        if (request === undefined) throw new Error('unexpected query');
        return request;
      },
      publish: value => published.push(value),
      report: error => reported.push(error),
    });
    const leader = owner.refresh();
    const follower = owner.refresh();

    predecessor.reject(new Error('stale failure'));
    await settle();
    expect(reported).toEqual([]);

    followUp.resolve('current');
    await Promise.all([leader, follower]);
    expect(published).toEqual(['current']);
    expect(reported).toEqual([]);
  });

  test.each(['success', 'failure'] as const)('retires a late %s after stop', async outcome => {
    const pending = deferred<string>();
    const published: string[] = [];
    const reported: unknown[] = [];
    const owner = new LatestQueryOwner({
      request: () => pending.promise,
      publish: value => published.push(value),
      report: error => reported.push(error),
    });
    const refresh = owner.refresh();

    owner.stop();
    if (outcome === 'success') pending.resolve('late');
    else pending.reject(new Error('late failure'));
    await refresh;

    expect(published).toEqual([]);
    expect(reported).toEqual([]);
  });
});
