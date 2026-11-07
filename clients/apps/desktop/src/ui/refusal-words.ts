/**
 * What a refusal that no retry changes says, for every screen that shows a host's state: the cause
 * and, where the host's type names it, the setting that fixes it. It lives beside the features, not
 * in one of them, because the run screens and the Resources tab both say it. The refusing side's
 * own words (platform, version, path) follow as Details.
 */
import type {RefusalCause} from '../state/index.js';

const CAUSE_WORD: Readonly<Record<RefusalCause, string>> = {
  'method-not-available': 'The app does not allow that call.',
  'invalid-request': 'The request is not valid.',
  'unsupported-platform': 'No host program for this platform.',
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
