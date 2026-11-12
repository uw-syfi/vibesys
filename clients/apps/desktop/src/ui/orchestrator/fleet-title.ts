import type {FleetRow} from '../../state/index.js';
import {runTitleParts} from './fleet-words.js';
import {genericRunTitle} from './generic-run-words.js';
import {setupTitle} from './setup/index.js';

/**
 * What a row is called, in the two halves its cell draws (the model or workload gives way, the
 * hardware stays): its task's title, or, in setup with no task, the state and the repository.
 */
export const rowTitleParts = (
  row: FleetRow,
): {readonly subject: string; readonly hardware: string} =>
  row.source === 'generic'
    ? {subject: genericRunTitle(row), hardware: ''}
    : row.setup
      ? {subject: setupTitle(row.repository), hardware: ''}
      : runTitleParts(row.card, row.name);

/** The row's title whole, for the pointer, for sorting and for assistive technology. */
export const rowTitle = (row: FleetRow): string => {
  const {subject, hardware} = rowTitleParts(row);
  return hardware === '' ? subject : `${subject} · ${hardware}`;
};
