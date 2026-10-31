/**
 * What the settings stores report as problems: the failures a person passes through, as data with
 * a stable identity, for the app's board of problems. A failure the screen holds with an action
 * beside it (a reading with Try again) is not here; it stays on the page.
 */
import type {MachineEntry} from '@vibesys/ipc';
import type {Problem} from '@vibesys/problems';
import type {MachineSettingsState, MachineView} from './machine-settings.js';
import {failureWords} from './machine-words.js';
import type {SettingsState} from './settings.js';

/** The Settings screen's save failure, until the next change. */
export function settingsProblems(state: SettingsState): readonly Problem[] {
  if (state.kind !== 'ready' || state.problem === null) return [];
  return [
    {id: 'settings/save', tone: 'err', title: 'Could not save the change.', detail: state.problem},
  ];
}

type Host = MachineEntry['host'];

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
 * The machine sections' failures on every machine: a cluster not removed, a login that did not
 * start. `label` names a machine for a title. The Add a cluster form holds its own failures (a
 * connection test that failed, a cluster not saved) beside its actions, so they are not here.
 */
export function machineSettingsProblems(
  state: MachineSettingsState,
  label: (host: Host) => string,
): readonly Problem[] {
  return Object.entries(state.views).flatMap(([key, view]) => {
    const host = key as Host;
    return [
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
