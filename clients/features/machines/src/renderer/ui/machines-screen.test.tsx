import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {
  initialMachines,
  type MachinesAction,
  type MachinesState,
  machinesStep,
} from '../../state/index.js';
import {MachinesScreen} from './machines-screen.js';

const RAW = 'connection down: ssh: connect to host nowhere port 22: Connection refused';

function drive(actions: readonly MachinesAction[]): MachinesState {
  let {state} = initialMachines();
  for (const action of actions) state = machinesStep(state, action).state;
  return state;
}

/** The Add form after Check and add failed with `result`. */
function failedAdd(result: Extract<MachinesAction, {type: 'saved'}>['result']): MachinesState {
  let {state} = initialMachines();
  const run = (action: MachinesAction) => {
    const step = machinesStep(state, action);
    state = step.state;
    return step;
  };
  run({type: 'open-add'});
  run({type: 'add-host', value: 'nowhere'});
  run({type: 'checkout', form: 'add', value: '~/vibesys'});
  const save = run({type: 'submit', form: 'add'}).requests.find(request => request.kind === 'save');
  run({type: 'saved', form: 'add', token: save?.token ?? 0, result});
  return state;
}

const screen = (state: MachinesState): string =>
  renderToStaticMarkup(
    <MachinesScreen
      state={state}
      dispatch={() => undefined}
      newRun={() => undefined}
      back={() => undefined}
      embedded
    />,
  );

test('an Add that cannot connect shows the status word under SSH host, the raw text on the pointer', () => {
  const state = failedAdd({ok: false, error: RAW, cause: 'link'});
  expect(state.adding?.problem).toMatchObject({field: 'host', cause: 'link', text: RAW});
  const html = screen(state);
  expect(html).toContain('>Unreachable<');
  expect(html).toContain(`title="${RAW}"`);
  expect(html).not.toContain(`>${RAW}<`);
});

test('every typed cause is worded, never the raw text', () => {
  for (const cause of ['link', 'auth', 'incompatible', 'run-gone', 'denied', 'capacity'] as const) {
    const html = screen(failedAdd({ok: false, error: RAW, cause}));
    expect(html).not.toContain(`>${RAW}<`);
    expect(html).toContain(`title="${RAW}"`);
  }
});

test('an Add that failed for its own reasons keeps its words under the checkout', () => {
  const state = failedAdd({ok: false, error: '~/vibesys on nowhere is not a directory.'});
  expect(state.adding?.problem).toMatchObject({field: 'checkout'});
  expect(screen(state)).toContain('>~/vibesys on nowhere is not a directory.<');
});

test('the header is a title, with the sentence on the pointer', () => {
  const html = screen(drive([]));
  expect(html).not.toContain('>Each machine runs VibeSys');
  expect(html).toContain('title="Each machine runs VibeSys');
});

test('an unreachable machine says its status word, with the connection text on the pointer', () => {
  const state = drive([
    {
      type: 'listed',
      token: 1,
      result: {ok: true, value: [{host: 'ssh:host-a', label: 'host-a', checkout: '~/vibesys'}]},
    },
    {
      type: 'reported',
      token: 2,
      host: 'ssh:host-a',
      result: {ok: true, value: {kind: 'unavailable', detail: RAW}},
    },
  ]);
  const html = screen(state);
  expect(html).toContain(`title="The app couldn’t connect: ${RAW}"`);
  expect(html).not.toContain(`>${RAW}<`);
});

test('the machines that could not be read offer to copy the details', () => {
  const html = screen({...initialMachines().state, listProblem: 'x'});
  expect(html).toContain('>Copy details<');
});
