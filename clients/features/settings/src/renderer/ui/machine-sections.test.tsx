import {expect, test} from 'bun:test';
import type {AgentLogin, ComputeSettings} from '@vibesys/ipc';
import {renderToStaticMarkup} from 'react-dom/server';
import type {MachineView} from '../../state/index.js';
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
