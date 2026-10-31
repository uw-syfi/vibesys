import {expect, test} from 'bun:test';
import type {AgentLogin, ComputeSettings} from '@vibesys/ipc';
import {renderToStaticMarkup} from 'react-dom/server';
import {type ClusterForm, type MachineView, NEW_FORM} from '../../state/index.js';
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

test('a machine too old for a section says Needs an update, with the detail on the pointer', () => {
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
    expect(html).toContain('title="The VibeSys on host-b is too old for this.');
  }
});

test('Agent logins draws labels, with each harness’s detail on its status', () => {
  const html = renderToStaticMarkup(<LoginsSection {...props} view={view({})} />);
  expect(prose(html)).toEqual([]);
  expect(html).toContain('title="Codex is not installed on host-b."');
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
  expect(unavailable).toContain('title="Claude Code has no login command to start."');
  const started = renderToStaticMarkup(
    <LoginsSection
      {...props}
      view={view({starts: {claude: {kind: 'started', instruction: 'Run claude login.'}}})}
    />,
  );
  expect(drawnText(started)).toContain('Login started');
  expect(drawnText(started)).toContain('Check again');
  expect(started).toContain('Run claude login.');
  expect(prose(started)).toEqual([]);
});

test('a cluster connection test is a status word in the form, whose settings changed is Test again', () => {
  const passed: ClusterForm = {
    ...NEW_FORM,
    test: {kind: 'passed', sent: 'not what the fields build', partitions: ['gpu']},
  };
  const html = renderToStaticMarkup(<ClustersSection {...props} view={view({form: passed})} />);
  expect(drawnText(html)).toContain('Test again');
  expect(html).toContain('title="The settings changed since the last test."');
  expect(html).not.toContain('Connection works');
});
