/**
 * What a refusal that no retry changes says, for every screen that shows a host's state: a short
 * label for a status chip, and the longer words for where a sentence has room. The cause and, where
 * the host's type names it, what to change. It lives beside the features, not in one of them,
 * because the run screens, the Runs list and the Resources tab all say it. The refusing side's own
 * words (platform, version, path) follow as Details.
 */
import type {RefusalCause} from '../state/index.js';

/**
 * The cause as a chip says it: a few words, one per cause, none the word of a link or a feed
 * ("Waiting", "Unavailable", "Not read yet"), so a host that cannot work is never mistaken for one
 * that has not answered yet.
 */
const CAUSE_LABEL: Readonly<Record<RefusalCause, string>> = {
  'method-not-available': 'Call not allowed',
  'invalid-request': 'Invalid request',
  'unsupported-platform': 'Platform unsupported',
  'host-program-missing': 'No host program',
  'host-prerequisite-missing': 'Host lacks tools',
  'state-network-filesystem': 'State on network disk',
  'state-unknown-filesystem': 'State disk not local',
  'state-refused': 'State refused',
  'config-refused': 'Config unreadable',
  'session-holder-missing': 'Agent runner missing',
  'session-holder-too-old': 'Agent runner too old',
  'skills-missing': 'Skills missing',
  'skills-too-old': 'Skills too old',
  'job-tool-missing': 'Job tool missing',
  'job-tool-too-old': 'Job tool too old',
};

const CAUSE_WORD: Readonly<Record<RefusalCause, string>> = {
  'method-not-available': 'The app does not allow that call.',
  'invalid-request': 'The request is not valid.',
  'unsupported-platform': 'No host program for this platform.',
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

export const causeLabel = (cause: RefusalCause): string => CAUSE_LABEL[cause];

export const causeWord = (cause: RefusalCause): string => CAUSE_WORD[cause];
