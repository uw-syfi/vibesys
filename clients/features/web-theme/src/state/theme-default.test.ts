import {expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {hostThemeDefaultMachine, hostThemeDefaultView, type WebThemeSession} from './index.js';

const SESSION: WebThemeSession = {
  start: () => Promise.resolve(),
  getState: () => ({hasSnapshot: true}),
  tuiDefaults: () => Promise.resolve({theme: 'dark'}),
};

test('model: exactly one resolver belongs to the resolving state', () => {
  checkMachine({
    machine: hostThemeDefaultMachine,
    input: undefined,
    event: (random, live) => {
      const settle = live.operations.flatMap(operation => [
        live.done(operation, random.pick(['dark', 'light', 'high-contrast-light'] as const)),
        live.error(operation, new Error('host unavailable')),
      ]);
      return random.pick([{type: 'resolve', session: SESSION}, ...settle, ...settle]);
    },
    invariants: snapshot => {
      const active = snapshot.matches('resolving');
      const view = hostThemeDefaultView(snapshot);
      return view.resolving === active
        ? []
        : [`resolving ${view.resolving} while state is ${String(snapshot.value)}`];
    },
    also: (_before, event, after) => {
      if (event.type === 'resolve') expect(hostThemeDefaultView(after).theme).toBe('dark');
    },
  });
});
