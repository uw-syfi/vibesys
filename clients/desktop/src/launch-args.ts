/**
 * What the app was asked to open, from its command line and environment, as a pure function.
 *
 * - No arguments: the host and run picker.
 * - `--host NAME`: the picker, on that SSH host. `--host NAME --instance ID` attaches to that host's
 *   detached run `ID`; `--instance ID` alone attaches to one on this machine.
 * - `--project PATH [-- RUN_ARGS...]`: start a detached run for that project on this machine and
 *   open the bundled UI on it.
 * - `--socket PATH`: open the bundled UI on a server already listening at that control socket.
 * - `VIBESYS_DESKTOP_URL` set: the legacy gateway window (`scripts/run-desktop.sh`), unchanged.
 *
 * Errors name the offending argument, so the shell can print them as they are.
 */
import {isAbsolute, resolve} from 'node:path';
import {type HostId, SettingsError, validAlias} from './host-settings.js';
import {type LaunchTarget, parseLaunchUrl} from './launch-url.js';

export type LaunchPlan =
  | {readonly kind: 'gateway'; readonly target: LaunchTarget}
  | {readonly kind: 'picker'; readonly host: HostId | null}
  | {readonly kind: 'instance'; readonly host: HostId; readonly instanceId: string}
  | {readonly kind: 'start'; readonly project: string; readonly runArgs: readonly string[]}
  | {readonly kind: 'attach'; readonly socketPath: string};

const USAGE =
  'usage: vibesys-desktop [--host NAME] [--instance ID]\n' +
  '       vibesys-desktop --project PATH [-- RUN_ARGS...]\n' +
  '       vibesys-desktop --socket PATH\n' +
  '       or VIBESYS_DESKTOP_URL=<gateway URL> vibesys-desktop';

export class LaunchError extends Error {
  override name = 'LaunchError';
}

type Option = '--project' | '--socket' | '--host' | '--instance';
const OPTIONS: readonly Option[] = ['--project', '--socket', '--host', '--instance'];
const PATH_OPTIONS: ReadonlySet<Option> = new Set(['--project', '--socket']);
const INSTANCE_ID = /^[0-9a-f]{12}$/;

/**
 * The plan for `argv` (the arguments after the app itself) and `gatewayUrl`
 * (`VIBESYS_DESKTOP_URL`). Relative paths resolve against `cwd`, the directory the user ran from.
 */
export function parseLaunch(
  argv: readonly string[],
  gatewayUrl: string | undefined,
  cwd: string,
): LaunchPlan {
  if (gatewayUrl !== undefined && gatewayUrl !== '') {
    if (argv.length > 0) throw new LaunchError('VIBESYS_DESKTOP_URL does not take arguments');
    return {kind: 'gateway', target: parseLaunchUrl(gatewayUrl)};
  }
  // `pnpm start -- ARGS` hands the app its `--` separator too. Run arguments only ever follow a
  // `--project`, so a leading separator carries nothing and is dropped.
  const options = argv[0] === '--' ? argv.slice(1) : argv;
  const {values, runArgs} = readOptions(options, cwd);
  const given = OPTIONS.filter(option => values.has(option));
  const project = values.get('--project');
  const socket = values.get('--socket');
  if ((project !== undefined || socket !== undefined) && given.length > 1) {
    throw new LaunchError(`pass only one of ${given.join(', ')}`);
  }
  if (runArgs.length > 0 && project === undefined) {
    throw new LaunchError(`only --project takes run arguments\n${USAGE}`);
  }
  if (project !== undefined) return {kind: 'start', project, runArgs};
  if (socket !== undefined) return {kind: 'attach', socketPath: socket};
  return hostPlan(values.get('--host'), values.get('--instance'));
}

/** The picker (on `alias` when given), or the run `instanceId` on that host or this machine. */
function hostPlan(alias: string | undefined, instanceId: string | undefined): LaunchPlan {
  const host: HostId | null = alias === undefined ? null : {kind: 'ssh', alias: hostAlias(alias)};
  if (instanceId === undefined) return {kind: 'picker', host};
  if (!INSTANCE_ID.test(instanceId)) {
    throw new LaunchError(`--instance needs a run id of 12 hex digits, not ${instanceId}`);
  }
  return {kind: 'instance', host: host ?? {kind: 'local'}, instanceId};
}

function hostAlias(alias: string): string {
  try {
    return validAlias(alias);
  } catch (error) {
    if (error instanceof SettingsError) throw new LaunchError(`--host: ${error.message}`);
    throw error;
  }
}

/** The options before `--` (paths resolved against `cwd`) and the arguments after it. */
function readOptions(
  argv: readonly string[],
  cwd: string,
): {values: Map<Option, string>; runArgs: readonly string[]} {
  const values = new Map<Option, string>();
  let index = 0;
  while (index < argv.length) {
    const argument = argv[index] as string;
    if (argument === '--') return {values, runArgs: argv.slice(index + 1)};
    const [flag, inline] = splitOption(argument);
    const option = OPTIONS.find(candidate => candidate === flag);
    if (option === undefined) throw new LaunchError(`unknown argument ${argument}\n${USAGE}`);
    if (values.has(option)) throw new LaunchError(`${option} is given twice`);
    values.set(option, optionValue(option, inline ?? argv[index + 1], cwd));
    index += inline === undefined ? 2 : 1;
  }
  return {values, runArgs: []};
}

/** An option's value, a path resolved against `cwd` for the path options. */
function optionValue(option: Option, value: string | undefined, cwd: string): string {
  const path = PATH_OPTIONS.has(option);
  if (value === undefined || value === '') {
    throw new LaunchError(`${option} needs ${path ? 'a path' : 'a value'}`);
  }
  return path && !isAbsolute(value) ? resolve(cwd, value) : value;
}

function splitOption(argument: string): [string, string | undefined] {
  const equals = argument.indexOf('=');
  if (!argument.startsWith('--') || equals < 0) return [argument, undefined];
  return [argument.slice(0, equals), argument.slice(equals + 1)];
}
