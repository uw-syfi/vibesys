/**
 * The pages of machine-scoped Settings, each as the steps a user takes to reach it, so the
 * browser journeys and the DOM tests look at the same screens. Steps are data: the browser
 * specs (`e2e/settings-machine-pages.ts`) and the DOM tests (`dom-steps.ts`) each read them with
 * their own hands, and neither imports the other's.
 */

/** The machines the world of these pages has: This Mac, fully set up; host-b, with nothing set up; host-c, whose VibeSys predates both features. */
export const SETTINGS_MACHINES = [
  {host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'},
  {host: 'ssh:host-b', label: 'host-b', checkout: '/home/me/vibesys'},
  {host: 'ssh:host-c', label: 'host-c', checkout: '/home/me/vibesys'},
] as const;

const SECTIONS = ['Clusters and clouds', 'Agent logins'] as const;
export type SectionName = (typeof SECTIONS)[number];

export type Step =
  /** Opens `section`, of the machine `machine` (its label in the selector) when one is named. */
  | {readonly open: SectionName; readonly machine?: string}
  /** Presses the first control of `role` named `name`. */
  | {readonly press: string; readonly role: 'button'}
  /** Chooses the radio named `name`. */
  | {readonly choose: string}
  /** Types `text` into the text field named `name`. */
  | {readonly fill: string; readonly text: string}
  /** Returns once `text` is on the page, in an alert when `inAlert`, or as the heading `heading`. */
  | {readonly see: string; readonly inAlert?: true; readonly heading?: true};

export interface SettingsPage {
  readonly name: string;
  readonly steps: readonly Step[];
}

const OPEN_FORM: readonly Step[] = [
  {open: 'Clusters and clouds', machine: 'host-b'},
  {press: 'Add a cluster', role: 'button'},
  {see: 'Add a Slurm cluster', heading: true},
];

/** Fills the Add form with a cluster whose alias decides how the test of its connection ends, and tests it. */
function tested(alias: string): readonly Step[] {
  return [
    ...OPEN_FORM,
    {fill: 'Name', text: 'lab-h100'},
    {fill: 'Workspace on the cluster', text: '/scratch/vibesys'},
    {fill: 'SSH host alias', text: alias},
    {press: 'Test connection', role: 'button'},
  ];
}

export const SETTINGS_PAGES: readonly SettingsPage[] = [
  {
    name: 'clusters',
    steps: [{open: 'Clusters and clouds'}, {see: 'cluster-m', heading: true}],
  },
  {
    name: 'clusters with the sections open',
    steps: [{open: 'Clusters and clouds'}, {see: 'cluster-m', heading: true}],
  },
  {
    name: 'clusters empty',
    steps: [{open: 'Clusters and clouds', machine: 'host-b'}, {see: 'No Slurm cluster yet'}],
  },
  {name: 'clusters add form', steps: OPEN_FORM},
  {
    name: 'clusters add form tested',
    steps: [...tested('lab-login'), {see: 'Connection works'}],
  },
  {
    name: 'clusters add form failed',
    steps: [...tested('no-workspace'), {see: 'The workspace is not usable', inAlert: true}],
  },
  {
    name: 'clusters login node',
    steps: [...OPEN_FORM, {choose: 'This machine is a login node'}],
  },
  {
    name: 'clusters too old',
    steps: [{open: 'Clusters and clouds', machine: 'host-c'}, {see: 'Needs an update'}],
  },
  {
    name: 'logins',
    steps: [{open: 'Agent logins'}, {see: 'Aider', heading: true}],
  },
  {
    name: 'logins started',
    steps: [
      {open: 'Agent logins', machine: 'host-b'},
      {press: 'Sign in', role: 'button'},
      {see: 'Login started'},
    ],
  },
];
