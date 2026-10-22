import {test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {loginMachine} from './login.js';

test('a login leaves running only for a terminal outcome and does not restart', () => {
  checkMachine({
    machine: loginMachine,
    input: undefined,
    event: (random, live) =>
      random.pick([
        {type: 'unavailable'},
        {type: 'cancel'},
        {type: 'lost'},
        {type: 'failed'},
        ...live.operations.map(operation =>
          live.done(operation, random.pick(['completed', 'lost'])),
        ),
        ...live.operations.map(operation => live.error(operation, new Error('watch failed'))),
      ]),
    invariants: snapshot => {
      if (snapshot.status === 'done' && snapshot.value === 'running')
        return ['a final login is still running'];
      return [];
    },
  });
});
