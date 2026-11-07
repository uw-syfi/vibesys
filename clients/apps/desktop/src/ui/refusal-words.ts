/**
 * What a refusal that no retry changes is called, for every screen that shows a host's state: the
 * cause as a label, and, in `causeHint`, the setting that fixes it, for a hover or a Details line,
 * never a sentence on the screen. It lives beside the features, not in one of them, because the run
 * screens, the Runs list and the Machines page all say it. The refusing side's own words (platform,
 * version, path) follow as Details.
 *
 * A label is a few words, one per cause, and none is the word of a link or a feed ("Waiting",
 * "Unavailable", "Not read yet"), so a host that cannot work is never mistaken for one that has not
 * answered yet.
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
  'session-holder-missing': 'Agent runner missing',
  'session-holder-too-old': 'Agent runner too old',
  'skills-missing': 'Skills missing',
  'skills-too-old': 'Skills too old',
  'job-tool-missing': 'Job tool missing',
  'job-tool-too-old': 'Job tool too old',
};

const CAUSE_HINT: Readonly<Record<RefusalCause, string>> = {
  'method-not-available': 'The app does not allow that call.',
  'invalid-request': 'The request is not valid.',
  'unsupported-platform': 'There is no host program for this platform.',
  'host-program-missing': 'This build has no host program for this platform.',
  'host-prerequisite-missing': 'The host lacks something the app needs to install its program.',
  'state-network-filesystem':
    'The host’s state directory is on a network disk. Set a state directory on a local disk.',
  'state-unknown-filesystem':
    'The host’s state directory is on a disk not known to be local. Set a state directory on a local disk.',
  'state-refused': 'The host refused its state directory.',
  'config-refused': 'The host cannot read its configuration.',
  'session-holder-missing': 'The host has no program to hold an agent session.',
  'session-holder-too-old': 'The host’s program for agent sessions is older than it needs.',
  'skills-missing': 'The host has no skills to give its agents.',
  'skills-too-old': 'The host’s skills are older than it needs.',
  'job-tool-missing': 'The host has no job tool to give its agents.',
  'job-tool-too-old': 'The host’s job tool is older than its tasks need.',
};

/** The cause as a label: a few words, for a chip or a list. */
export const causeWord = (cause: RefusalCause): string => CAUSE_WORD[cause];

/** The longer account behind a cause's label, for a hover. */
export const causeHint = (cause: RefusalCause): string => CAUSE_HINT[cause];
