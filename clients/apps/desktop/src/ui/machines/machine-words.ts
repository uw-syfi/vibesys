/**
 * What a machine's reading is called when it is not current, or absent: the `MachineWhy` a
 * `MachineView`'s `stale` or `Missing.why` carries, and the `AbsenceCause` a single section (CPU,
 * memory or GPU) carries when the host's snapshot has no reading for it. They are said on hover over
 * the machine's cells, never as a chip: the host's one status chip (`host-status-words.ts`) is the
 * only status a row shows. `disconnected` has no word of its own for that reason; the hover says the
 * link is down in plain words, and the readings dim.
 */
import type {StatusTone} from '@vibesys/ui';
import type {AbsenceCause, MachineWhy} from '../../state/index.js';

export type MachineChipWhy = Exclude<MachineWhy, 'disconnected'>;

export interface MachineWhyWord {
  readonly label: string;
  readonly tone: StatusTone;
  readonly hint: string;
}

const WHY_WORD: Readonly<Record<MachineChipWhy, MachineWhyWord>> = {
  updating: {
    label: 'Updating',
    tone: 'neutral',
    hint: "A new stream is reading the host's current state.",
  },
  'not-updating': {
    label: 'Not updating',
    tone: 'warn',
    hint: 'The stream is open, but no new reading has arrived within its bound.',
  },
  silent: {
    label: 'No answer',
    tone: 'warn',
    hint: "No frame arrived from the host's stream within its bound.",
  },
  'no-readings': {
    label: 'No readings',
    tone: 'neutral',
    hint: "The host's snapshot holds nothing to show.",
  },
  unsupported: {
    label: 'Not supported',
    tone: 'neutral',
    hint: 'This host cannot sample its machine, or predates this feature.',
  },
  'over-limit': {
    label: 'Over limit',
    tone: 'warn',
    hint: 'The stream broke a limit the topic holds every reader to.',
  },
  'read-failed': {
    label: 'Read failed',
    tone: 'warn',
    hint: "The host's sampler failed or stopped.",
  },
  'bad-readings': {
    label: 'Bad readings',
    tone: 'err',
    hint: "What the host sent broke the topic's contract.",
  },
  failed: {
    label: 'Failed',
    tone: 'err',
    hint: 'The host refused the call, or it failed for another reason.',
  },
};

/**
 * What a hover says of why a machine has no current reading, for any `MachineWhy`: the account of its
 * chip word, and for `disconnected` (which has no chip, the status chip says it) the plain cause.
 */
export const whyHint = (why: MachineWhy): string => {
  if (why === 'disconnected')
    return 'The link to this machine is down, so it has no live readings.';
  const word = WHY_WORD[why];
  return `${word.label}. ${word.hint}`;
};

/** The word for a `MachineWhy` other than `disconnected`, which carries no word of its own. */
export const machineWhyWord = (why: MachineChipWhy): MachineWhyWord => WHY_WORD[why];

/** Every why a chip speaks for: the keys of the one map above, which the type keeps exhaustive. */
export const MACHINE_CHIP_WHYS: readonly MachineChipWhy[] = Object.keys(
  WHY_WORD,
) as MachineChipWhy[];

/** A short, lowercase note for a gauge whose section the host has no reading of: `", no tool"`. */
const ABSENCE_NOTE: Readonly<Record<AbsenceCause, string>> = {
  'no-tool': 'no tool',
  'read-failed': 'not read',
  'timed-out': 'timed out',
  unparsed: 'not understood',
  unverified: 'not supported',
};

export const absenceNote = (cause: AbsenceCause): string => ABSENCE_NOTE[cause];

/** Every absence a note speaks for: the keys of the one map above, which the type keeps exhaustive. */
export const ABSENCE_CAUSES: readonly AbsenceCause[] = Object.keys(ABSENCE_NOTE) as AbsenceCause[];
