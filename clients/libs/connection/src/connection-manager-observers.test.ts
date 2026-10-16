import {expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {ConnectionManager} from './index.js';
import {FakeLink} from './testing/index.js';

const GPU = {kind: 'ssh', alias: 'gpu'} as const;

test('synchronous listener and diagnostic failures cannot change replacement ownership', async () => {
  const link = new FakeLink();
  const logged: string[] = [];
  await using manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: message => {
      logged.push(message);
      if (message.includes('could not notify a connection listener')) {
        throw new Error('synchronous logger failure');
      }
    },
  });
  const previous = await manager.acquire(GPU, '/old');
  await previous.connection.ready();
  const heard: string[] = [];
  const ownership: boolean[] = [];
  manager.onConnection(() => {
    throw new Error('synchronous listener failure');
  });
  manager.onConnection((host, connection) => {
    heard.push(`${host}:${connection.where}`);
    ownership.push(manager.existing(host) === connection);
  });

  const current = await manager.acquire(GPU, '/new');

  expect(manager.existing('ssh:gpu')).toBe(current.connection);
  expect(heard).toEqual(['ssh:gpu:/new on fake']);
  expect(ownership).toEqual([true]);
  expect(logged.filter(message => message.includes('could not notify'))).toEqual([
    '/new on fake: could not notify a connection listener: synchronous listener failure',
  ]);
  await previous[Symbol.asyncDispose]();
  expect(previous.connection.phase.kind).toBe('closed');
  expect(current.connection.phase.kind).not.toBe('closed');
  await current[Symbol.asyncDispose]();
  expect(current.connection.phase.kind).toBe('closed');
  await manager[Symbol.asyncDispose]();
  expect(link.released).toBe(1);
});

test('rejected listener and diagnostic promises are terminally contained', async () => {
  const link = new FakeLink();
  const logged: string[] = [];
  await using manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: message => {
      logged.push(message);
      if (message.includes('could not notify a connection listener')) {
        return Promise.reject(new Error('asynchronous logger failure'));
      }
      return undefined;
    },
  });
  const heard: string[] = [];
  manager.onConnection(() => Promise.reject(new Error('asynchronous listener failure')));
  manager.onConnection((host, connection) => {
    heard.push(`${host}:${connection.where}`);
  });

  const lease = await manager.acquire(GPU, '/new');
  await Promise.resolve();

  expect(manager.existing('ssh:gpu')).toBe(lease.connection);
  expect(heard).toEqual(['ssh:gpu:/new on fake']);
  expect(logged.filter(message => message.includes('could not notify'))).toEqual([
    '/new on fake: could not notify a connection listener: asynchronous listener failure',
  ]);
  await lease[Symbol.asyncDispose]();
  await manager[Symbol.asyncDispose]();
  expect(link.released).toBe(1);
});

test('connection delivery snapshots registration for the current event', async () => {
  const link = new FakeLink();
  await using manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
  const heard: string[] = [];
  let firstDelivery = true;
  let unsubscribeSelf = (): void => {};
  let unsubscribeLater = (): void => {};
  const self = (_host: string, connection: {readonly where: string}): void => {
    heard.push(`self:${connection.where}`);
    if (!firstDelivery) return;
    firstDelivery = false;
    unsubscribeSelf();
    unsubscribeSelf = manager.onConnection(self);
    unsubscribeLater();
  };
  unsubscribeSelf = manager.onConnection(self);
  unsubscribeLater = manager.onConnection((_host, connection) => {
    heard.push(`later:${connection.where}`);
  });

  const first = await manager.acquire(GPU, '/one');
  const second = await manager.acquire(GPU, '/two');
  try {
    expect(heard).toEqual(['self:/one on fake', 'later:/one on fake', 'self:/two on fake']);
  } finally {
    await second[Symbol.asyncDispose]();
    await first[Symbol.asyncDispose]();
  }
});
