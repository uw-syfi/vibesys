/** Plain words for generic run-index states; generated enum names never reach a screen. */
import {type StatusTone, withoutPaths} from '@vibesys/ui';
import type {
  GenericDiscoveryStatus,
  GenericFleetRow,
  GenericRunIndexUnavailable,
  GenericRunState,
} from '../../state/index.js';
import type {Word} from './words.js';

const STATE_WORD: Readonly<Record<GenericRunState, Word>> = {
  preparing: {label: 'Preparing', tone: 'live'},
  starting: {label: 'Starting', tone: 'live'},
  running: {label: 'Running', tone: 'live'},
  held: {label: 'Held', tone: 'warn'},
  parking: {label: 'Parking', tone: 'warn'},
  stopping: {label: 'Stopping', tone: 'warn'},
  parked: {label: 'Parked', tone: 'neutral'},
  finished: {label: 'Finished', tone: 'ok'},
  failed: {label: 'Failed', tone: 'err'},
  stopped: {label: 'Stopped', tone: 'warn'},
  alive: {label: 'Active', tone: 'live'},
  interrupted: {label: 'Interrupted', tone: 'warn'},
  ended: {label: 'Ended', tone: 'ok'},
  unknown: {label: 'Status unavailable', tone: 'neutral'},
};

/** The project folder names a generic run when the host supplied one; otherwise its id does. */
export const genericRunTitle = (row: Pick<GenericFleetRow, 'projectRoot'>): string =>
  row.projectRoot === null ? 'Run' : withoutPaths(row.projectRoot);

export const genericRunWord = (row: Pick<GenericFleetRow, 'state'>): Word => STATE_WORD[row.state];

export const genericIndexWords = (row: GenericFleetRow): string =>
  row.indexFreshness === 'current' ? 'Up to date' : 'Last known';

const INDEX_UNAVAILABLE: Readonly<Record<GenericRunIndexUnavailable, string>> = {
  stale: 'The host has not supplied a complete run-index snapshot.',
  unsupported: 'This host does not support generic run discovery.',
  'over-limit': 'The host run index is too large to read safely.',
  'source-failed': 'The host could not read its run index.',
  internal: 'The host could not serve its run index.',
  invalid: 'The host supplied an invalid run index.',
  transport: 'The run index could not be read from the host.',
};

export const genericIndexUnavailableWord = (reason: GenericRunIndexUnavailable): string =>
  INDEX_UNAVAILABLE[reason];

export interface GenericDiscoveryIssue {
  readonly label: string;
  readonly tone: StatusTone;
  readonly hint: string;
}

/** A host-filter issue for the generic index; a current index has no issue to show. */
export function genericDiscoveryIssue(
  status: GenericDiscoveryStatus,
): GenericDiscoveryIssue | null {
  switch (status.kind) {
    case 'current':
      return null;
    case 'opening':
      return {
        label: 'Generic index · Opening',
        tone: 'neutral',
        hint: 'Waiting for the generic run index to provide its first complete snapshot.',
      };
    case 'stale':
      return {
        label: 'Generic index · Stale',
        tone: 'warn',
        hint: 'Generic runs are shown from the last complete snapshot.',
      };
    case 'unavailable':
      return {
        label: 'Generic index · Unavailable',
        tone: 'err',
        hint: genericIndexUnavailableWord(status.reason),
      };
  }
}

/** The whole typed account behind the row's short state label. */
export const genericRunHint = (row: GenericFleetRow): string =>
  [
    genericRunWord(row).label,
    genericIndexWords(row),
    row.needsUser ? 'Needs your input' : '',
    row.projectRoot === null ? 'Project unavailable' : row.projectRoot,
    `Run ${row.runId}`,
  ]
    .filter(part => part !== '')
    .join('\n');
