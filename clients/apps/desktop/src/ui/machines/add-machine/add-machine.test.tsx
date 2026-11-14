import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import {act, type JSX, useState} from 'react';
import type {MachineActions} from '../../../core/index.js';
import type {MachineRow} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {memoryMachineActions} from '../../../testing/settings/machine-actions.js';
import {MachinesScreen} from '../machines-screen.js';
import {AddMachinePage} from './add-machine-page.js';
import {type MachineDispatch as Dispatch, useMachineControls} from './machine-controls.js';

const dom = installDom().window;

const ALIASES = ['host-a', 'host-b', 'my-host-c'];

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
  readonly saveRepositories?: (
    host: string,
    folder: string,
    done: (error: string | null) => void,
  ) => void;
}): JSX.Element {
  const machines = useMachineControls(props.actions, props.dispatch);
  const [adding, setAdding] = useState(false);
  if (machines === undefined) throw new Error('machine actions are required');
  return adding ? (
    <AddMachinePage
      origin="machines"
      controls={machines.add}
      close={() => setAdding(false)}
      openRuns={() => undefined}
      {...(props.saveRepositories === undefined ? {} : {saveRepositories: props.saveRepositories})}
    />
  ) : (
    <MachinesScreen
      rows={rows(props.hosts)}
      controls={machines}
      addMachine={() => setAdding(true)}
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
  const input = container.querySelector<HTMLInputElement>('input.vs-input');
  if (input === null) throw new Error('no input is open');
  return input;
};

const namedField = (container: HTMLElement, label: string): HTMLElement => {
  const found = [...container.querySelectorAll<HTMLElement>('.vs-field')].find(
    item => item.querySelector('.vs-field-label')?.textContent === label,
  );
  if (found === undefined) throw new Error(`no field named ${label}`);
  return found;
};

const fieldInput = (container: HTMLElement, label: string): HTMLInputElement => {
  const input = namedField(container, label).querySelector<HTMLInputElement>('input');
  if (input === null) throw new Error(`field ${label} has no input`);
  return input;
};

const segment = (container: HTMLElement, label: string): HTMLElement => {
  const control = container.querySelector<HTMLElement>(`label[data-label="${label}"]`);
  if (control === null) throw new Error(`no segment named ${label}`);
  return control;
};

describe('adding and removing a machine', () => {
  it.effect('offers matches as it is typed, adds the one picked, and closes', () =>
    Effect.gen(function* () {
      const memory = memoryMachineActions({aliases: ALIASES});
      const {actions} = memory;
      const {dispatch, flush} = queue();
      const screen = mount(<Screen actions={actions} dispatch={dispatch} hosts={['local']} />);
      screen.press(screen.control('Add machine or server'));
      yield* flush;
      expect(document.activeElement).toBe(box(screen.container));
      type(box(screen.container), 'ho');
      yield* flush;
      expect(screen.controls('host-a')).toHaveLength(1);
      expect(screen.controls('my-host-c')).toHaveLength(1);
      expect(screen.controls('host-b')[0]).toBeDefined();
      screen.press(screen.control('host-b'));
      screen.press(screen.control('Add machine'));
      yield* flush;
      expect(memory.saved()).toEqual(['ssh:host-b']);
      expect(screen.container.querySelector('input')).toBeNull();
      screen.unmount();
    }),
  );

  it.effect('adds on Enter, reads a refusal under the input, and closes on Cancel', () =>
    Effect.gen(function* () {
      const memory = memoryMachineActions({aliases: ALIASES});
      const {actions} = memory;
      const {dispatch, flush} = queue();
      const screen = mount(<Screen actions={actions} dispatch={dispatch} hosts={['local']} />);
      screen.press(screen.control('Add machine or server'));
      yield* flush;
      screen.press(segment(screen.container, 'Paste SSH command'));
      type(box(screen.container), 'ssh -X host-a');
      yield* flush;
      key(box(screen.container), 'Enter');
      yield* flush;
      expect(screen.text()).toContain('-X is not accepted');
      expect(memory.saved()).toEqual([]);
      type(box(screen.container), 'ssh -p 2222 user@host-a -J jump-a');
      yield* flush;
      key(box(screen.container), 'Enter');
      yield* flush;
      expect(memory.saved()).toEqual(['ssh:user@host-a']);
      screen.press(screen.control('Add machine or server'));
      yield* flush;
      screen.press(screen.control('Cancel'));
      expect(screen.container.querySelector('input')).toBeNull();
      screen.unmount();
    }),
  );

  it.effect('removes a machine from its row, and Undo puts it back', () =>
    Effect.gen(function* () {
      const memory = memoryMachineActions({aliases: ALIASES, saved: ['host-a']});
      const {actions} = memory;
      const {dispatch, flush} = queue();
      const screen = mount(
        <Screen actions={actions} dispatch={dispatch} hosts={['local', 'ssh:host-a']} />,
      );
      expect(screen.controls('Remove This Mac from this app')).toHaveLength(0);
      screen.press(screen.control('Remove host-a from this app'));
      yield* flush;
      expect(memory.saved()).toEqual([]);
      expect(screen.text()).toContain('Removed host-a');
      screen.press(screen.control('Undo'));
      yield* flush;
      expect(memory.saved()).toEqual(['ssh:host-a']);
      expect(screen.controls('Undo')).toHaveLength(0);
      screen.unmount();
    }),
  );

  it.effect('puts each direct validation error under its own field', () =>
    Effect.gen(function* () {
      const memory = memoryMachineActions({aliases: ALIASES});
      const {dispatch, flush} = queue();
      const screen = mount(
        <Screen actions={memory.actions} dispatch={dispatch} hosts={['local']} />,
      );
      screen.press(screen.control('Add machine or server'));
      yield* flush;
      screen.press(segment(screen.container, 'Connection details'));
      type(fieldInput(screen.container, 'Host name or address *'), 'host-a');
      type(fieldInput(screen.container, 'Port'), '70000');

      expect(namedField(screen.container, 'Port').textContent).toContain(
        'Port must be a whole number from 1 to 65535.',
      );
      expect(namedField(screen.container, 'Host name or address *').textContent).not.toContain(
        'Port must be',
      );

      type(fieldInput(screen.container, 'Port'), '22');
      type(fieldInput(screen.container, 'Repositories folder'), 'relative/repos');
      expect(namedField(screen.container, 'Repositories folder').textContent).toContain(
        'Repositories: absolute path or ~',
      );
      expect(namedField(screen.container, 'Host name or address *').textContent).not.toContain(
        'Repositories:',
      );
      expect(memory.saved()).toEqual([]);
      screen.unmount();
    }),
  );

  it.effect('keeps a repository save refusal on the workspace field', () =>
    Effect.gen(function* () {
      const memory = memoryMachineActions({aliases: ALIASES});
      const {dispatch, flush} = queue();
      const screen = mount(
        <Screen
          actions={memory.actions}
          dispatch={dispatch}
          hosts={['local']}
          saveRepositories={(_host, _folder, done) => done('Folder cannot be reached.')}
        />,
      );
      screen.press(screen.control('Add machine or server'));
      yield* flush;
      type(fieldInput(screen.container, 'SSH host or alias *'), 'host-a');
      type(fieldInput(screen.container, 'Repositories folder'), '~/work');
      screen.press(screen.control('Add machine'));
      yield* flush;

      expect(namedField(screen.container, 'Repositories folder').textContent).toContain(
        'Folder cannot be reached.',
      );
      expect(namedField(screen.container, 'SSH host or alias *').textContent).not.toContain(
        'Folder cannot be reached.',
      );
      expect(memory.saved()).toEqual(['ssh:host-a']);
      screen.unmount();
    }),
  );
});
