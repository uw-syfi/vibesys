import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import {act, type JSX} from 'react';
import type {MachineActions, MachineUndo} from '../../../core/index.js';
import {
  type MachineRow,
  matchAliases,
  SshCommandError,
  targetFromInput,
} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {MachinesScreen} from '../machines-screen.js';
import {type MachineDispatch as Dispatch, useMachineControls} from './machine-controls.js';

const dom = installDom().window;

const ALIASES = ['host-a', 'host-b', 'my-host-c'];

/** `MachineActions` over a set of saved keys in memory, parsing with the same functions the main process uses. */
function fakeActions() {
  const saved = new Set<string>();
  const actions: MachineActions = {
    match: query => Effect.succeed(matchAliases(ALIASES, query)),
    add: input =>
      Effect.sync(() => {
        try {
          const host = `ssh:${targetFromInput(input).destination}`;
          const created = !saved.has(host);
          saved.add(host);
          return {kind: 'added', host, created} as const;
        } catch (error) {
          if (error instanceof SshCommandError)
            return {kind: 'refused', reason: error.message} as const;
          throw error;
        }
      }),
    remove: host =>
      Effect.sync(() => {
        saved.delete(host);
        return {kind: 'removed', host, undo: {host} as unknown as MachineUndo} as const;
      }),
    undo: host =>
      Effect.sync(() => {
        saved.add(host);
        return {kind: 'added', host, created: true} as const;
      }),
  };
  return {actions, saved};
}

/** A dispatch that holds each effect until the test runs it, so a test runs them as an effect. */
function queue() {
  const pending: {run: () => Effect.Effect<void>}[] = [];
  const dispatch: Dispatch = (effect, then) => {
    pending.push({
      run: () =>
        Effect.map(effect, value => {
          act(() => then(value));
        }),
    });
  };
  const flush = Effect.gen(function* () {
    while (pending.length > 0) yield* pending.shift()?.run() ?? Effect.void;
  });
  return {dispatch, flush};
}

const rows = (keys: readonly string[]): MachineRow[] =>
  keys.map(key => ({
    key,
    label: key === 'local' ? 'This Mac' : key.slice(4),
    status: {kind: 'live', cause: null, staleForMs: null},
  }));

function Screen(props: {
  readonly actions: MachineActions;
  readonly dispatch: Dispatch;
  readonly hosts: readonly string[];
}): JSX.Element {
  const machines = useMachineControls(props.actions, props.dispatch);
  return (
    <MachinesScreen
      rows={rows(props.hosts)}
      {...(machines === undefined ? {} : {controls: machines})}
    />
  );
}

/** A key event of the test's DOM, which its own `Event` type does not match. */
const keyboard = (type: string, name: string): Event =>
  new (dom().KeyboardEvent)(type, {key: name, bubbles: true, cancelable: true}) as unknown as Event;

/** Types into the focused input as a person does: the value changes and React hears an input event. */
function type(input: HTMLInputElement, text: string): void {
  act(() => {
    const set = Object.getOwnPropertyDescriptor(dom().HTMLInputElement.prototype, 'value')?.set;
    input.focus();
    set?.call(input, text);
    // happy-dom has no `oninput`, so React watches a focused input's value on key events instead.
    input.dispatchEvent(keyboard('keyup', 'a'));
  });
}

function key(target: HTMLElement, name: string): void {
  act(() => {
    target.focus();
    target.dispatchEvent(keyboard('keydown', name));
  });
}

const box = (container: HTMLElement): HTMLInputElement => {
  const input = container.querySelector('input');
  if (input === null) throw new Error('no input is open');
  return input;
};

describe('adding and removing a machine', () => {
  it.effect('offers matches as it is typed, adds the one picked, and closes', () =>
    Effect.gen(function* () {
      const {actions, saved} = fakeActions();
      const {dispatch, flush} = queue();
      const screen = mount(<Screen actions={actions} dispatch={dispatch} hosts={['local']} />);
      screen.press(screen.control('Add machine'));
      yield* flush;
      expect(document.activeElement).toBe(box(screen.container));
      type(box(screen.container), 'ho');
      yield* flush;
      expect(screen.controls('host-a')).toHaveLength(1);
      expect(screen.controls('my-host-c')).toHaveLength(1);
      expect(screen.controls('host-b')[0]).toBeDefined();
      screen.press(screen.control('host-b'));
      yield* flush;
      expect([...saved]).toEqual(['ssh:host-b']);
      expect(screen.container.querySelector('input')).toBeNull();
      screen.unmount();
    }),
  );

  it.effect('adds on Enter, reads a refusal under the input, and closes on Escape', () =>
    Effect.gen(function* () {
      const {actions, saved} = fakeActions();
      const {dispatch, flush} = queue();
      const screen = mount(<Screen actions={actions} dispatch={dispatch} hosts={['local']} />);
      screen.press(screen.control('Add machine'));
      yield* flush;
      type(box(screen.container), 'ssh -X host-a');
      yield* flush;
      key(box(screen.container), 'Enter');
      yield* flush;
      expect(screen.text()).toContain('-X is not accepted');
      expect(saved.size).toBe(0);
      type(box(screen.container), 'ssh -p 2222 user@host-a -J jump-a');
      yield* flush;
      key(box(screen.container), 'Enter');
      yield* flush;
      expect([...saved]).toEqual(['ssh:user@host-a']);
      screen.press(screen.control('Add machine'));
      yield* flush;
      // The first Escape closes the list of matches, the second the panel.
      key(box(screen.container), 'Escape');
      key(box(screen.container), 'Escape');
      expect(screen.container.querySelector('input')).toBeNull();
      screen.unmount();
    }),
  );

  it.effect('removes a machine from its row, and Undo puts it back', () =>
    Effect.gen(function* () {
      const {actions, saved} = fakeActions();
      saved.add('ssh:host-a');
      const {dispatch, flush} = queue();
      const screen = mount(
        <Screen actions={actions} dispatch={dispatch} hosts={['local', 'ssh:host-a']} />,
      );
      expect(screen.controls('Remove This Mac')).toHaveLength(0);
      screen.press(screen.control('Remove host-a'));
      yield* flush;
      expect(saved.size).toBe(0);
      expect(screen.text()).toContain('Removed host-a');
      screen.press(screen.control('Undo'));
      yield* flush;
      expect([...saved]).toEqual(['ssh:host-a']);
      expect(screen.controls('Undo')).toHaveLength(0);
      screen.unmount();
    }),
  );
});
