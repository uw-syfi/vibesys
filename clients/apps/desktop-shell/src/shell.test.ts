import {expect, test} from 'bun:test';
import type {Screen} from '@vibesys/feature-app-shell/state';
import type {PreferencesView, WelcomeBridge, WelcomeEvent} from '@vibesys/ipc';
import {boardToasts} from '@vibesys/problems';
import type {ThemeMode, ThemeName} from '@vibesys/ui/tokens';
import fc from 'fast-check';
import {createShellStores} from './shell.js';

const ignore = (): void => undefined;

/** A window address that holds no zoom: these tests open no run. */
const NO_ADDRESS = {
  fragment: () => '',
  push: ignore,
  onRestore: () => ignore,
  onEscape: () => ignore,
};

const STORED: PreferencesView = {
  appearance: {mode: 'system', light: 'mist', dark: 'midnight'},
  notifications: {enabled: true},
  archived: [],
};

/** The bridge calls the shell's stores make, answering from memory. */
function fakeBridge(): WelcomeBridge {
  const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});
  const ignore = (_listener: (event: WelcomeEvent<never>) => void): void => undefined;
  const bridge: Partial<WelcomeBridge> = {
    overview: () =>
      ok({
        eventCursor: 0,
        settingsProblem: null,
        recent: [],
        skippedRecent: [],
        listings: [],
        chrome: {mode: 'welcome', attached: null, stops: {}},
        host: 'local',
      }),
    preferences: () => ok(STORED),
    setAppearance: appearance => ok({...STORED, appearance}),
    setNotifications: enabled => ok({...STORED, notifications: {enabled}}),
    archive: () => ok(STORED),
    machines: () => ok([]),
    runDigests: () => ok({kind: 'unsupported' as const}),
    onListing: ignore,
    onRecent: ignore,
    onPreferences: ignore,
    onOpenSettings: ignore,
    openSettings: () => ok(null),
    describeMachine: () => ok({kind: 'unsupported' as const}),
    harnesses: () => ok({kind: 'unsupported' as const}),
    examples: () => ok({kind: 'unsupported' as const}),
  };
  return bridge as WelcomeBridge;
}

test('the page follows the stored theme for the system mode, and each change of it', async () => {
  const painted: ThemeName[] = [];
  let systemListener: (mode: ThemeMode) => void = () => undefined;
  const stores = createShellStores(
    fakeBridge(),
    {mode: 'dark', onChange: listener => (systemListener = listener)},
    {setAttribute: (_name, value) => painted.push(value)},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
  await Promise.resolve();
  await Promise.resolve();
  expect(painted.at(-1)).toBe('midnight');
  systemListener('light');
  expect(painted.at(-1)).toBe('mist');
  stores.settings.dispatch({
    type: 'change',
    change: {kind: 'appearance', appearance: {mode: 'dark', light: 'mist', dark: 'terminal'}},
  });
  expect(painted.at(-1)).toBe('terminal');
});

test('New run opened for a machine starts on it; opened plainly, it keeps its machine', () => {
  const stores = createShellStores(
    fakeBridge(),
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
  const select = (screen: Screen): void => stores.sidebar.store.dispatch({type: 'select', screen});
  const machine = () => stores.newRun.getState().context.form.machine;
  expect(machine()).toBe('local');
  select({kind: 'machines'});
  select({kind: 'new-run', machine: 'ssh:gpu-box'});
  expect(machine()).toBe('ssh:gpu-box');
  select({kind: 'settings'});
  select({kind: 'new-run'});
  expect(machine()).toBe('ssh:gpu-box');
});

test('regression: Run again opens New run with the options the run was started with', () => {
  const stores = createShellStores(
    fakeBridge(),
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
  const run = {host: 'ssh:gpu-box', runId: '20261015-163047'} as const;
  stores.sidebar.store.dispatch({
    type: 'recent',
    recent: [
      {
        ...run,
        hostLabel: 'gpu-box',
        project: '/p/queue',
        task: 'mpmc',
        instanceId: null,
        attachedAt: 1,
        start: {
          harness: null,
          options: {rounds: 3, time_limit_s: 600, results: 'publish_github', roles: []},
          agents: {},
          measure_on: {kind: 'same_machine'},
          args: ['--max-in-flight', '3'],
        },
      },
    ],
  });
  stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'new-run', again: run}});
  const {form} = stores.newRun.getState().context;
  expect(form.machine).toBe('ssh:gpu-box');
  expect(form.task).toBe('mpmc');
  expect(form.budget).toEqual({rounds: 3, timeLimitS: 600, results: 'publish_github'});
  expect(form.advanced).toBe('--max-in-flight 3');
});

test('Settings opened for a machine and section lands there, reads the machine, and returns to New run', async () => {
  const read: string[] = [];
  const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});
  const bridge = {
    ...fakeBridge(),
    machines: () =>
      ok([
        {host: 'local', label: 'This Mac', checkout: '/c'},
        {host: 'ssh:gpu-box', label: 'gpu-box', checkout: '/c'},
      ]),
    compute: (host: string) => {
      read.push(`compute ${host}`);
      return ok({kind: 'unsupported' as const});
    },
    agentLogins: (host: string) => {
      read.push(`logins ${host}`);
      return ok({kind: 'unsupported' as const});
    },
  } as WelcomeBridge;
  const stores = createShellStores(
    bridge,
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
  const select = (screen: Screen): void => stores.sidebar.store.dispatch({type: 'select', screen});
  select({kind: 'new-run', machine: 'ssh:gpu-box'});
  await settle();
  const before = stores.newRun.getState();
  select({kind: 'settings', machine: 'ssh:gpu-box', section: 'logins'});
  expect(stores.machineSettings.getState()).toMatchObject({
    machine: 'ssh:gpu-box',
    section: 'logins',
  });
  // The machines New run was for are read once each; Settings read gpu-box already.
  expect(read.sort()).toEqual([
    'compute local',
    'compute ssh:gpu-box',
    'logins local',
    'logins ssh:gpu-box',
  ]);
  stores.sidebar.store.dispatch({type: 'back-from-settings'});
  expect(stores.sidebar.store.getState().selection).toMatchObject({kind: 'new-run'});
  expect(stores.newRun.getState()).toBe(before);
});

test('Settings opened plainly keeps the machine and section it was on', async () => {
  const stores = createShellStores(
    fakeBridge(),
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
  stores.machineSettings.dispatch({type: 'focus', host: 'ssh:gpu-box', section: 'clusters'});
  stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'settings'}});
  expect(stores.machineSettings.getState()).toMatchObject({
    machine: 'ssh:gpu-box',
    section: 'clusters',
  });
});

/** Lets the replies of a bridge that answers at once reach the stores. */
async function settle(): Promise<void> {
  for (let tick = 0; tick < 8; tick += 1) await Promise.resolve();
}

test("New run offers what the machine's Settings data says, and returning from Settings keeps the form", async () => {
  const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});
  const settings = {
    config_path: '/home/me/.config/vibesys/config.toml',
    clusters: [{cluster: {name: 'lab-h100', reach: {kind: 'login_node'}}, source: 'user'}],
    modal: {token_present: false},
  };
  const bridge = {
    ...fakeBridge(),
    compute: (host: string) =>
      ok(
        host === 'ssh:gpu-box'
          ? {kind: 'offered' as const, value: settings}
          : {kind: 'unsupported' as const},
      ),
    agentLogins: () => ok({kind: 'unsupported' as const}),
  } as unknown as WelcomeBridge;
  const stores = createShellStores(
    bridge,
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
  const select = (screen: Screen): void => stores.sidebar.store.dispatch({type: 'select', screen});
  select({kind: 'new-run', machine: 'ssh:gpu-box'});
  await settle();
  expect(stores.newRun.getState().context.compute).toEqual({
    kind: 'offered',
    value: settings,
  } as never);
  stores.newRun.dispatch({type: 'measure', target: {kind: 'slurm', cluster: 'lab-h100'}});
  stores.newRun.dispatch({type: 'advanced', advanced: '--max-in-flight 3'});
  const before = stores.newRun.getState().context;
  // The link on the Where page: Settings on that machine's clusters, and Back to the same form.
  select({kind: 'settings', machine: 'ssh:gpu-box', section: 'clusters'});
  stores.sidebar.store.dispatch({type: 'back-from-settings'});
  await settle();
  const after = stores.newRun.getState().context;
  expect(after.form).toEqual(before.form);
  expect(after.at).toBe(before.at);
  expect(after.answers).toEqual(before.answers);
});

test('property: cold-start data arrival never navigates away from Runs', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.shuffledSubarray(['overview', 'recent', 'listing', 'preferences', 'machines'] as const, {
        minLength: 5,
        maxLength: 5,
      }),
      async order => {
        const overview = Promise.withResolvers<Awaited<ReturnType<WelcomeBridge['overview']>>>();
        const preferences =
          Promise.withResolvers<Awaited<ReturnType<WelcomeBridge['preferences']>>>();
        const machines = Promise.withResolvers<Awaited<ReturnType<WelcomeBridge['machines']>>>();
        let recent: Parameters<WelcomeBridge['onRecent']>[0] = () => {};
        let listing: Parameters<WelcomeBridge['onListing']>[0] = () => {};
        const opened = [
          {
            host: 'local' as const,
            hostLabel: 'Local',
            project: '/project',
            runId: 'run-a',
            task: null,
            instanceId: null,
            attachedAt: 1,
          },
        ];
        const listed = {
          kind: 'listed' as const,
          host: 'local' as const,
          unreadable: [],
          unverified: [],
          live: [],
          ended: [],
        };
        const bridge = {
          ...fakeBridge(),
          overview: () => overview.promise,
          preferences: () => preferences.promise,
          machines: () => machines.promise,
          onRecent: (receive: typeof recent) => {
            recent = receive;
          },
          onListing: (receive: typeof listing) => {
            listing = receive;
          },
          projectRuns: async () => ({
            ok: true as const,
            value: [],
          }),
        } as WelcomeBridge;
        const stores = createShellStores(
          bridge,
          {mode: 'light', onChange: () => {}},
          {setAttribute: () => {}},
          () => () => {},
          {
            messages: {addEventListener: ignore, removeEventListener: ignore},
            address: NO_ADDRESS,
            now: () => 0,
            session: () => {
              throw new Error('No user opened a run');
            },
          },
          {},
        );
        for (const event of order) {
          if (event === 'overview')
            overview.resolve({
              ok: true,
              value: {
                eventCursor: 1,
                recent: opened,
                skippedRecent: [],
                listings: [listed],
              },
            });
          else if (event === 'recent') recent({sequence: 2, value: opened});
          else if (event === 'listing') listing({sequence: 3, value: listed});
          else if (event === 'preferences') preferences.resolve({ok: true, value: STORED});
          else machines.resolve({ok: true, value: []});
          await settle();
          expect(stores.sidebar.store.getState().selection).toEqual({kind: 'runs'});
          expect(stores.newRun.getState().context.started).toBeNull();
        }
      },
    ),
    {numRuns: 30, seed: 341807},
  );
});

test("a machine's Settings reading that failed while it was unreachable is read again when it comes up", async () => {
  const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});
  const link = () =>
    Promise.resolve({
      ok: false as const,
      error: 'down',
      authNeeded: false,
      cause: 'link' as const,
    });
  let up = false;
  const bridge = {
    ...fakeBridge(),
    compute: () => (up ? ok({kind: 'unsupported' as const}) : link()),
    agentLogins: () => (up ? ok({kind: 'unsupported' as const}) : link()),
  } as WelcomeBridge;
  const stores = createShellStores(
    bridge,
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
  const settleReads = async (): Promise<void> => {
    for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
  };
  const view = () => stores.machineSettings.getState().views['ssh:gpu-box'];
  stores.machineSettings.dispatch({type: 'refresh', host: 'ssh:gpu-box', what: 'compute'});
  await settleReads();
  expect(view()?.compute.kind).toBe('failed');
  const listing = {
    kind: 'unavailable' as const,
    host: 'ssh:gpu-box',
    connection: {kind: 'offline' as const, detail: 'down'},
  };
  stores.sidebar.store.dispatch({type: 'listing', listing});
  up = true;
  stores.sidebar.store.dispatch({
    type: 'listing',
    listing: {...listing, kind: 'listed', live: [], ended: [], unreadable: [], unverified: []},
  });
  await settleReads();
  expect(view()?.compute.kind).toBe('ready');
  expect(view()?.logins.kind).toBe('ready');
});

const APPEARANCE = {mode: 'dark', light: 'mist', dark: 'terminal'} as const;

function storesOver(bridge: WelcomeBridge) {
  return createShellStores(
    bridge,
    {mode: 'light', onChange: () => undefined},
    {setAttribute: () => undefined},
    () => () => undefined,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: NO_ADDRESS,
      now: () => 0,
      session: () => {
        throw new Error('this test opens no run');
      },
    },
    {},
  );
}

test('a failure a screen passes through reaches the window’s board, once, until it clears', async () => {
  const refusing = {
    ...fakeBridge(),
    setAppearance: () =>
      Promise.resolve({ok: false as const, error: 'The disk is full.', authNeeded: false}),
  } as WelcomeBridge;
  const stores = storesOver(refusing);
  await settle();
  const shown = () =>
    boardToasts(stores.problems.getState()).map(toast => `${toast.title} · ${toast.detail}`);
  expect(shown()).toEqual([]);
  const save = (): void =>
    stores.settings.dispatch({
      type: 'change',
      change: {kind: 'appearance', appearance: APPEARANCE},
    });
  save();
  await settle();
  expect(shown()).toEqual(['Could not save the change. · The disk is full.']);
  stores.problems.dispatch({type: 'dismiss', id: 'settings/save'});
  expect(shown()).toEqual([]);
  // The next change clears the problem and fails again: it is new, so it is shown again.
  save();
  await settle();
  expect(shown()).toEqual(['Could not save the change. · The disk is full.']);
});

test('machines that cannot be listed are reported with the machines store’s words', async () => {
  const refusing = {
    ...fakeBridge(),
    machines: () =>
      Promise.resolve({ok: false as const, error: 'hosts.json is unreadable.', authNeeded: false}),
  } as WelcomeBridge;
  const stores = storesOver(refusing);
  await settle();
  expect(boardToasts(stores.problems.getState())).toMatchObject([
    {id: 'machines/list', tone: 'err', title: 'The machines could not be read'},
  ]);
});
