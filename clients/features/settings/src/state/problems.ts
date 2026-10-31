/**
 * What the settings stores report as problems: the failures a person passes through, as data with
 * a stable identity, for the app's board of problems. A failure the screen holds with an action
 * beside it (a reading with Try again) is not here; it stays on the page.
 */
import type {MachineEntry} from '@vibesys/ipc';
import type {Problem} from '@vibesys/problems';
import {type ClusterForm, stale} from './cluster-form.js';
import type {MachineSettingsState, MachineView} from './machine-settings.js';
import {failureWords} from './machine-words.js';
import type {SettingsState} from './settings.js';

const TEST_STAGE_WORDS = {
  reach: 'Could not reach the cluster',
  scheduler: 'The cluster’s scheduler did not answer',
  workspace: 'The workspace is not usable',
} as const;

/** The Settings screen's save failure, until the next change. */
export function settingsProblems(state: SettingsState): readonly Problem[] {
  if (state.kind !== 'ready' || state.problem === null) return [];
  return [
    {id: 'settings/save', tone: 'err', title: 'Could not save the change.', detail: state.problem},
  ];
}

type Host = MachineEntry['host'];

function formProblems(host: Host, form: ClusterForm): Problem[] {
  const problems: Problem[] = [];
  if (form.problem !== null && form.problem.field === null) {
    problems.push({
      id: `settings/cluster-save/${host}`,
      tone: 'err',
      title: 'The cluster was not saved.',
      detail: form.problem.text,
    });
  }
  if (form.test.kind === 'failed' && !stale(form)) {
    problems.push({
      id: `settings/cluster-test/${host}`,
      tone: 'err',
      title: TEST_STAGE_WORDS[form.test.stage],
      detail: form.test.detail,
    });
  }
  return problems;
}

function loginProblems(host: Host, machine: string, starts: MachineView['starts']): Problem[] {
  return Object.entries(starts).flatMap(([provider, progress]) => {
    if (progress.kind !== 'failed') return [];
    const {body, hint} = failureWords(progress);
    return [
      {
        id: `settings/login-start/${host}/${provider}`,
        tone: 'err' as const,
        title: `Could not start the login on ${machine}.`,
        detail: hint === undefined ? body : `${body}\n${hint}`,
      },
    ];
  });
}

/**
 * The machine sections' failures on every machine: a cluster not saved or removed, a connection
 * test that failed (until the settings change and it must be run again), a login that did not
 * start. `label` names a machine for a title.
 */
export function machineSettingsProblems(
  state: MachineSettingsState,
  label: (host: Host) => string,
): readonly Problem[] {
  return Object.entries(state.views).flatMap(([key, view]) => {
    const host = key as Host;
    return [
      ...(view.form === null ? [] : formProblems(host, view.form)),
      ...(view.removeProblem === null
        ? []
        : [
            {
              id: `settings/cluster-remove/${host}`,
              tone: 'err' as const,
              title: 'The cluster was not removed.',
              detail: view.removeProblem,
            },
          ]),
      ...loginProblems(host, label(host), view.starts),
    ];
  });
}
