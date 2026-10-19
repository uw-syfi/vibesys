/**
 * What the app was asked to open, from its command line and environment, as a pure function.
 *
 * - No arguments: the main window.
 * - `--host NAME --instance ID` opens that SSH host's detached run `ID` in a window of its own;
 *   `--instance ID` alone opens one on this machine.
 * - `--project PATH [-- RUN_ARGS...]`: start a detached run for that project on this machine and
 *   open it in a window of its own.
 *
 * Errors name the offending argument, so the shell can print them as they are.
 */
import {isAbsolute, resolve} from 'node:path';
import {type HostId, HostValidationError, validAlias} from '@vibesys/hosts';
import {isInstanceId} from '@vibesys/runs';

export type LaunchPlan =
  | {readonly kind: 'main-window'}
  | {readonly kind: 'instance'; readonly host: HostId; readonly instanceId: string}
  | {readonly kind: 'start'; readonly project: string; readonly runArgs: readonly string[]};

const USAGE =
  'usage: vibesys-desktop [[--host NAME] --instance ID]\n' +
  '       vibesys-desktop --project PATH [-- RUN_ARGS...]';

/** The environment variable that points the app at a user-data directory of its own. */
export const USER_DATA_ENV = 'VIBESYS_USER_DATA_DIR';

/**
 * The user-data directory `env` asks for, or null for Electron's default. A development launcher
 * sets it so a build under test keeps its settings apart from the installed app's. It must be an
 * absolute path: a relative one would mean the directory of whichever process reads it.
 */
export function userDataOverride(env: Readonly<Record<string, string | undefined>>): string | null {
  const value = env[USER_DATA_ENV];
  if (value === undefined || value === '') return null;
  if (!isAbsolute(value)) {
    throw new LaunchError(`${USER_DATA_ENV} must be an absolute path, not ${value}`);
  }
  return value;
}

export class LaunchError extends Error {
  override name = 'LaunchError';
}

type Option = '--project' | '--host' | '--instance';
const OPTIONS: readonly Option[] = ['--project', '--host', '--instance'];
const PATH_OPTIONS: ReadonlySet<Option> = new Set(['--project']);

/**
 * The plan for `argv` (the arguments after the app itself). Relative paths resolve against `cwd`,
 * the directory the user ran from.
 */
export function parseLaunch(argv: readonly string[], cwd: string): LaunchPlan {
  // `pnpm start -- ARGS` hands the app its `--` separator too. Run arguments only ever follow a
  // `--project`, so a leading separator carries nothing and is dropped.
  const options = argv[0] === '--' ? argv.slice(1) : argv;
  const {values, runArgs} = readOptions(options, cwd);
  const given = OPTIONS.filter(option => values.has(option));
  const project = values.get('--project');
  if (project !== undefined && given.length > 1) {
    throw new LaunchError(`pass only one of ${given.join(', ')}`);
  }
  if (runArgs.length > 0 && project === undefined) {
    throw new LaunchError(`only --project takes run arguments\n${USAGE}`);
  }
  if (project !== undefined) return {kind: 'start', project, runArgs};
  return hostPlan(values.get('--host'), values.get('--instance'));
}

/** The main window, or the run `instanceId` on `alias`'s host or this machine. */
function hostPlan(alias: string | undefined, instanceId: string | undefined): LaunchPlan {
  const host: HostId | null = alias === undefined ? null : {kind: 'ssh', alias: hostAlias(alias)};
  if (instanceId === undefined) {
    if (host !== null) throw new LaunchError(`--host needs --instance\n${USAGE}`);
    return {kind: 'main-window'};
  }
  if (!isInstanceId(instanceId)) {
    throw new LaunchError(`--instance needs a run id of 12 hex digits, not ${instanceId}`);
  }
  return {kind: 'instance', host: host ?? {kind: 'local'}, instanceId};
}

function hostAlias(alias: string): string {
  try {
    return validAlias(alias);
  } catch (error) {
    if (error instanceof HostValidationError) throw new LaunchError(`--host: ${error.message}`);
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
