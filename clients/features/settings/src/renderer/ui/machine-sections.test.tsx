import {expect, test} from 'bun:test';
import type {AgentLogin, ClusterTest, ComputeSettings} from '@vibesys/ipc';
import {renderToStaticMarkup} from 'react-dom/server';
import {
  type ClusterForm,
  INITIAL_MACHINE_SETTINGS,
  type MachineSettingsAction,
  type MachineSettingsCall,
  type MachineSettingsState,
  type MachineView,
  machineSettingsStep,
} from '../../state/index.js';
import {ClustersSection} from './clusters-section.js';
import {LoginsSection} from './logins-section.js';

/**
 * The screens say labels, numbers and status words; prose goes on the pointer. A text run drawn
 * on the screen is a label when it is a few words and does not end like a sentence. Text kept for
 * assistive technology only (`data-quiet`) is not drawn.
 */
function drawnText(html: string): string[] {
  const drawn = html.replace(/<span[^>]*data-quiet[^>]*>.*?<\/span>/g, '');
  return [...drawn.matchAll(/>([^<>]+)</g)].map(match => (match[1] ?? '').trim()).filter(Boolean);
}

function prose(html: string): string[] {
  return drawnText(html).filter(text => text.split(/\s+/).length > 6 || /[.!?]$/.test(text));
}

const COMPUTE: ComputeSettings = {
  config_path: '/home/me/.config/vibesys/compute.toml',
  clusters: [
    {
      cluster: {
        name: 'lab-h100',
        workspace: '/scratch/vibesys',
        reach: {kind: 'ssh', host: 'lab-login'},
        sbatch_options: [],
      },
      source: 'vibesys',
    },
    {
      cluster: {
        name: 'campus',
        workspace: '/work/vibesys',
        reach: {kind: 'login_node'},
        sbatch_options: [],
      },
      source: 'slurm_toml',
    },
  ],
  modal: {token_present: false},
};

const LOGINS: readonly AgentLogin[] = [
  {
    provider: 'claude',
    provider_name: 'Claude Code',
    installed: true,
    state: 'signed_out',
    api_key_variables: ['ANTHROPIC_API_KEY'],
    detail: 'Not logged in. Run claude and sign in.',
    can_start_login: true,
  },
  {
    provider: 'codex',
    provider_name: 'Codex',
    installed: false,
    state: 'unknown',
    api_key_variables: [],
    detail: 'Codex is not installed on host-b.',
    can_start_login: false,
  },
];

const view = (patch: Partial<MachineView>): MachineView => ({
  compute: {kind: 'ready', offered: {kind: 'offered', value: COMPUTE}},
  logins: {kind: 'ready', offered: {kind: 'offered', value: LOGINS}},
  form: null,
  removing: null,
  removeProblem: null,
  starts: {},
  ...patch,
});

const props = {host: 'ssh:host-b', machine: 'host-b', dispatch: () => undefined};

test('Clusters and clouds draws labels, with where it is stored on the pointer', () => {
  const html = renderToStaticMarkup(<ClustersSection {...props} view={view({})} />);
  expect(prose(html)).toEqual([]);
  expect(html).toContain('title="Stored on host-b"');
  expect(html).not.toContain('Stored on host-b in');
});

test('Clusters and clouds says how a cluster is reached in a few words', () => {
  const text = drawnText(renderToStaticMarkup(<ClustersSection {...props} view={view({})} />));
  expect(text).toContain('SSH · lab-login');
  expect(text).toContain('Login node');
});

test('a machine too old for a section says Needs an update, with the detail as its description', () => {
  const old = view({
    compute: {kind: 'ready', offered: {kind: 'unsupported'}},
    logins: {kind: 'ready', offered: {kind: 'unsupported'}},
  });
  for (const html of [
    renderToStaticMarkup(<ClustersSection {...props} view={old} />),
    renderToStaticMarkup(<LoginsSection {...props} view={old} />),
  ]) {
    expect(prose(html)).toEqual([]);
    expect(drawnText(html)).toContain('Needs an update');
    expect(html).toContain('aria-description="The VibeSys on host-b is too old for this.');
  }
});

test('Agent logins draws labels, with each harness’s detail on its status', () => {
  const html = renderToStaticMarkup(<LoginsSection {...props} view={view({})} />);
  expect(prose(html)).toEqual([]);
  expect(html).toContain('aria-description="Codex is not installed on host-b."');
  expect(html).toContain('title="Or set an API key on host-b"');
});

test('a section that could not be read says so as a status word with Try again, the detail on the pointer', () => {
  const failed = view({
    compute: {kind: 'failed', error: 'the helper hung up'},
    logins: {kind: 'failed', error: 'the helper hung up'},
  });
  for (const html of [
    renderToStaticMarkup(<ClustersSection {...props} view={failed} />),
    renderToStaticMarkup(<LoginsSection {...props} view={failed} />),
  ]) {
    expect(prose(html)).toEqual([]);
    expect(drawnText(html)).toContain('Not read');
    expect(drawnText(html)).toContain('Try again');
    expect(html).toContain('the helper hung up');
    expect(html).not.toContain('>the helper hung up<');
    expect(html).toContain('data-tone="err"');
  }
});

test('a login the helper cannot start, or has started, is a status word on its card', () => {
  const unavailable = renderToStaticMarkup(
    <LoginsSection
      {...props}
      view={view({starts: {claude: {kind: 'unavailable', reason: 'no_login_command'}}})}
    />,
  );
  expect(drawnText(unavailable)).toContain('Cannot sign in');
  expect(unavailable).toContain('aria-description="Claude Code has no login command to start."');
  const started = renderToStaticMarkup(
    <LoginsSection
      {...props}
      view={view({
        starts: {
          claude: {
            kind: 'started',
            instruction: 'Open https://example.com/device and enter the code AB12',
          },
        },
      })}
    />,
  );
  expect(drawnText(started)).toContain('Login started');
  expect(drawnText(started)).toContain('Check again');
  // The next step is on the page as the two things to read off it, not only a description.
  expect(drawnText(started)).toContain('https://example.com/device');
  expect(drawnText(started)).toContain('AB12');
  expect(started).not.toContain('aria-description="Open https://');
  expect(prose(started)).toEqual([]);
});

const HOST = 'ssh:host-b';

/** The Add a cluster form after a user filled it in and the helper answered its test with `answer`. */
function formAnswered(
  answer:
    | {readonly ok: true; readonly value: ClusterTest}
    | {readonly ok: false; readonly error: string},
  after: readonly MachineSettingsAction[] = [],
): ClusterForm {
  let state: MachineSettingsState = INITIAL_MACHINE_SETTINGS;
  const issued: MachineSettingsCall[] = [];
  const act = (action: MachineSettingsAction): void => {
    const step = machineSettingsStep(state, action);
    state = step.state;
    issued.push(...step.requests);
  };
  act({type: 'focus', host: HOST, section: 'clusters'});
  act({type: 'open-form', host: HOST});
  for (const [key, value] of [
    ['name', 'lab'],
    ['host', 'lab-login'],
    ['workspace', '/w'],
  ] as const) {
    act({type: 'field', host: HOST, key, value});
  }
  act({type: 'test', host: HOST});
  const test = issued.find(call => call.kind === 'test');
  if (test === undefined) throw new Error('the form did not ask for a test');
  act({type: 'tested', token: test.token, host: HOST, result: answer});
  for (const action of after) act(action);
  const form = state.views[HOST]?.form;
  if (form === null || form === undefined) throw new Error('the form is not open');
  return form;
}

const alerts = (html: string): number => html.match(/role="alert"/g)?.length ?? 0;

for (const [stage, says] of [
  ['reach', 'Could not reach the cluster'],
  ['scheduler', 'The cluster’s scheduler did not answer'],
  ['workspace', 'The workspace is not usable'],
] as const) {
  test(`a connection test that failed at ${stage} is said once, in the form, with why`, () => {
    const form = formAnswered({
      ok: true,
      value: {outcome: 'failed', stage, detail: 'not writable'},
    });
    const html = renderToStaticMarkup(<ClustersSection {...props} view={view({form})} />);
    expect(alerts(html)).toBe(1);
    expect(drawnText(html).filter(text => text === says)).toHaveLength(1);
    expect(drawnText(html)).toContain('not writable');
  });
}

test('a failure that names no field is said in the form; one that names a field stays under it', () => {
  const unnamed = formAnswered({ok: false, error: 'the helper did not answer'});
  const unnamedHtml = renderToStaticMarkup(
    <ClustersSection {...props} view={view({form: unnamed})} />,
  );
  expect(alerts(unnamedHtml)).toBe(1);
  expect(drawnText(unnamedHtml)).toContain('the helper did not answer');

  const named = formAnswered({ok: false, error: 'cluster.workspace: not a directory'});
  const namedHtml = renderToStaticMarkup(<ClustersSection {...props} view={view({form: named})} />);
  expect(alerts(namedHtml)).toBe(0);
  expect(drawnText(namedHtml)).toContain('cluster.workspace: not a directory');
});

test('a test result of settings that have since changed is not shown as a failure', () => {
  const form = formAnswered(
    {ok: true, value: {outcome: 'failed', stage: 'workspace', detail: 'not writable'}},
    [{type: 'field', host: HOST, key: 'workspace', value: '/other'}],
  );
  const html = renderToStaticMarkup(<ClustersSection {...props} view={view({form})} />);
  expect(alerts(html)).toBe(0);
  expect(drawnText(html)).toContain('Test again');
  expect(html).toContain('aria-description="The settings changed since the last test."');
});

test('a connection test that passed is not an alert', () => {
  const form = formAnswered({ok: true, value: {outcome: 'passed', hardware: {}, partitions: []}});
  const html = renderToStaticMarkup(<ClustersSection {...props} view={view({form})} />);
  expect(alerts(html)).toBe(0);
  expect(drawnText(html)).toContain('Connection works');
});
