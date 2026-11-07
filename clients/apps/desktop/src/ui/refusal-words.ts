/**
 * What a refusal that no retry changes is called, for every screen that shows a host's state: the
 * cause as a label, and, in `causeHint`, the setting that fixes it, for a hover or a Details line,
 * never a sentence on the screen. It lives beside the features, not in one of them, because the run
 * screens and the Resources tab both say it. The refusing side's own words (platform, version, path)
 * follow as Details.
 */
import type {RefusalCause} from '../state/index.js';

const CAUSE_WORD: Readonly<Record<RefusalCause, string>> = {
  'method-not-available': 'Call not allowed',
  'invalid-request': 'Invalid request',
  'unsupported-platform': 'Platform unsupported',
  'host-program-missing': 'No host program',
  'host-prerequisite-missing': 'Prerequisite missing',
  'state-network-filesystem': 'State on network disk',
  'state-unknown-filesystem': 'State disk unknown',
  'state-refused': 'State refused',
  'config-refused': 'Config unreadable',
};

const CAUSE_HINT: Readonly<Record<RefusalCause, string>> = {
  'method-not-available': 'The app does not allow that call.',
  'invalid-request': 'The request is not valid.',
  'unsupported-platform': 'There is no host program for this platform.',
  'host-program-missing': 'This build has no host program for this platform.',
  'host-prerequisite-missing': 'The host lacks something the app needs to install its program.',
  'state-network-filesystem':
    'The host\u2019s state directory is on a network disk. Set a state directory on a local disk.',
  'state-unknown-filesystem':
    'The host\u2019s state directory is on a disk not known to be local. Set a state directory on a local disk.',
  'state-refused': 'The host refused its state directory.',
  'config-refused': 'The host cannot read its configuration.',
};

export const causeWord = (cause: RefusalCause): string => CAUSE_WORD[cause];

/** The longer account behind a cause's label, for a hover. */
export const causeHint = (cause: RefusalCause): string => CAUSE_HINT[cause];
