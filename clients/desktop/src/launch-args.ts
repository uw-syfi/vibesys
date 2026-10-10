/**
 * What the app was asked to open, from its command line and environment, as a pure function.
 *
 * - `VIBESYS_DESKTOP_URL` set: the legacy gateway window (`scripts/run-desktop.sh`), unchanged.
 * - `--project PATH [-- RUN_ARGS...]`: start a local server for that project and open the bundled
 *   UI on it.
 * - `--socket PATH`: open the bundled UI on a server already listening at that control socket.
 *
 * Errors name the offending argument, so the shell can print them as they are.
 */
import {isAbsolute, resolve} from 'node:path';
import {type LaunchTarget, parseLaunchUrl} from './launch-url.js';

export type LaunchPlan =
  | {readonly kind: 'gateway'; readonly target: LaunchTarget}
  | {readonly kind: 'start'; readonly serverArgs: readonly string[]}
  | {readonly kind: 'attach'; readonly socketPath: string};

const USAGE =
  'usage: vibesys-desktop (--project PATH [-- RUN_ARGS...] | --socket PATH)\n' +
  '       or VIBESYS_DESKTOP_URL=<gateway URL> vibesys-desktop';

export class LaunchError extends Error {
  override name = 'LaunchError';
}

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
  const {paths, runArgs} = readOptions(options, cwd);
  const project = paths.get('--project');
  const socket = paths.get('--socket');
  if (project !== undefined && socket !== undefined) {
    throw new LaunchError('pass --project or --socket, not both');
  }
  if (socket !== undefined) {
    if (runArgs.length > 0) throw new LaunchError('--socket does not take run arguments');
    return {kind: 'attach', socketPath: socket};
  }
  if (project === undefined) throw new LaunchError(`pass --project or --socket\n${USAGE}`);
  return {kind: 'start', serverArgs: ['--project', project, ...runArgs]};
}

type PathOption = '--project' | '--socket';

/** The path options before `--`, resolved against `cwd`, and the arguments after it. */
function readOptions(
  argv: readonly string[],
  cwd: string,
): {paths: Map<PathOption, string>; runArgs: readonly string[]} {
  const paths = new Map<PathOption, string>();
  let index = 0;
  while (index < argv.length) {
    const argument = argv[index] as string;
    if (argument === '--') return {paths, runArgs: argv.slice(index + 1)};
    const [flag, inline] = splitOption(argument);
    if (flag !== '--project' && flag !== '--socket') {
      throw new LaunchError(`unknown argument ${argument}\n${USAGE}`);
    }
    const value = requiredPath(flag, inline ?? argv[index + 1]);
    index += inline === undefined ? 2 : 1;
    if (paths.has(flag)) throw new LaunchError(`${flag} is given twice`);
    paths.set(flag, isAbsolute(value) ? value : resolve(cwd, value));
  }
  return {paths, runArgs: []};
}

function requiredPath(flag: PathOption, value: string | undefined): string {
  if (value === undefined || value === '') throw new LaunchError(`${flag} needs a path`);
  return value;
}

function splitOption(argument: string): [string, string | undefined] {
  const equals = argument.indexOf('=');
  if (!argument.startsWith('--') || equals < 0) return [argument, undefined];
  return [argument.slice(0, equals), argument.slice(equals + 1)];
}
