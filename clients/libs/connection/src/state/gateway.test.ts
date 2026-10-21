import {test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
// test-isolation: exercise the gateway's pure operation/deadline ownership across every control mode.
import {gatewayMachine} from './gateway.js';

test('gateway model bounds retries and owns all dials and backoff delays', () => {
  checkMachine({
    machine: gatewayMachine,
    input: undefined,
    event: (random, live) =>
      random.pick([
        {type: 'lost'},
        {type: 'close'},
        ...live.operations.map(operation =>
          live.done(
            operation,
            random.pick([
              {ok: true},
              {ok: false, fatal: false, error: 'link lost'},
              {ok: false, fatal: true, error: 'hash mismatch'},
            ]),
          ),
        ),
        ...live.operations.map(operation => live.error(operation, new Error('dial failed'))),
        ...live.delays.map(delay => live.fire(delay)),
      ]),
    invariants: snapshot => (snapshot.context.attempt <= 4 ? [] : ['retry bound exceeded']),
  });
});
