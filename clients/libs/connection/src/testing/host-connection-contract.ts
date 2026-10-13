/**
 * The `HostConnection` contract suite: what every host must do the same way, run against the
 * `FakeHelper` and against the real helper of a local checkout (`VIBESYS_REAL_CONTRACTS=1`). A case
 * that needs a host the suite cannot arrange for one implementation does not belong here; it goes
 * to that implementation's own tests.
 */
import {describe, expect, test} from 'bun:test';
import type {InstanceList} from '@vibesys/contracts/host-helper';
import {ConnectionError, HelperRequestError} from '../shell/helper-channel.js';
import type {HostConnection} from '../shell/host-connection.js';

export interface ContractHost {
  /** A connection to a host with a working checkout and an empty registry. */
  working(): HostConnection;
  /** A connection whose checkout is not a VibeSys checkout. */
  broken(): HostConnection;
}

/** Resolve once `connection` reaches a phase of `kind` (now or later). */
export function phaseReached(connection: HostConnection, kind: string): Promise<void> {
  if (connection.phase.kind === kind) return Promise.resolve();
  return new Promise(resolve => {
    const stop = connection.onPhase(phase => {
      if (phase.kind !== kind) return;
      stop();
      resolve();
    });
  });
}

async function rejection(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error('resolved instead of rejecting');
}

export function hostConnectionContract(name: string, host: ContractHost, skip = false): void {
  describe.skipIf(skip)(`HostConnection contract (${name})`, () => {
    test('becomes ready, answers requests, and pushes its registry', async () => {
      await using connection = host.working();
      await connection.ready();
      await connection.request({type: 'ping'});
      const listings: InstanceList[] = [];
      const pushed = new Promise<void>(resolve => {
        connection.watchInstances(listing => {
          listings.push(listing);
          resolve();
        });
      });
      await pushed;
      expect(listings[0]?.instances).toEqual([]);
      expect(connection.checkoutRoot).not.toBeNull();
    });

    test('a request the helper refuses fails with its code and path, and the host stays ready', async () => {
      await using connection = host.working();
      const error = await rejection(
        connection.request({type: 'tasks.list', project: '/nonexistent-vibesys-contract/p'}),
      );
      expect(error).toBeInstanceOf(HelperRequestError);
      expect((error as HelperRequestError).code).toBe('project_missing');
      const missing = await rejection(
        connection.request({type: 'paths.complete', prefix: '/nonexistent-vibesys-contract/'}),
      );
      expect((missing as HelperRequestError).code).toBe('path_missing');
      expect(connection.phase.kind).toBe('ready');
    });

    test('a run channel to a run that is not there fails run-gone, and the host stays ready', async () => {
      await using connection = host.working();
      const error = await rejection(connection.openRun('0123456789ab', 'run-control'));
      expect(error).toBeInstanceOf(ConnectionError);
      expect((error as ConnectionError).reason).toBe('run-gone');
      expect(connection.phase.kind).toBe('ready');
    });

    test('a checkout that is not VibeSys stops as incompatible, saying why', async () => {
      await using connection = host.broken();
      const error = await rejection(connection.ready());
      expect((error as ConnectionError).reason).toBe('incompatible');
      // The bootstrap's own report, never a parse error of its output.
      expect(error.message).toContain('is not a directory');
      expect(connection.phase.kind).toBe('incompatible');
    });

    test('closing releases the connection; later requests fail and nothing reopens', async () => {
      const connection = host.working();
      await connection.ready();
      await connection[Symbol.asyncDispose]();
      expect(connection.phase.kind).toBe('closed');
      expect(await rejection(connection.request({type: 'ping'}))).toBeInstanceOf(ConnectionError);
    });
  });
}
