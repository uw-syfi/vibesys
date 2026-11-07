/**
 * What a host must have for the app to install its program there, as one table: the one script
 * that checks every item, and the reading of what that script printed. Provisioning asks it once,
 * before the copy, so a host that lacks something is refused with the name of what it lacks
 * (`DesktopRefusal.HOST_PREREQUISITE_MISSING`) instead of failing the install with an exit code
 * that reads as a lost link and is redialed for ever. The table lists exactly what the install
 * scripts in `main/gateway-provision.ts` run: add a command there, add it here.
 */

/** One thing the host must have: what the report calls it, and the shell test that it has it. */
interface Prerequisite {
  readonly name: string;
  /** A POSIX shell test that succeeds when the host has it. */
  readonly has: string;
}

/** A command the host must provide. */
const command = (name: string): Prerequisite => ({
  name,
  has: `command -v ${name} >/dev/null 2>&1`,
});

/** The directory under the host's home that the install checks it can create and write. */
const PROBE_DIRECTORY = '.vibesys/gateway';

/** Everything the install runs or needs, each named as the report names it. */
export const HOST_PREREQUISITES: readonly Prerequisite[] = [
  command('uname'),
  command('mkdir'),
  command('gzip'),
  command('chmod'),
  command('mv'),
  command('rm'),
  command('wc'),
  command('tr'),
  command('cut'),
  {
    name: 'sha256sum or shasum',
    has: 'command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1',
  },
  {
    name: 'a writable home directory',
    has: `[ -n "$HOME" ] && mkdir -p "$HOME/${PROBE_DIRECTORY}" 2>/dev/null && [ -w "$HOME/${PROBE_DIRECTORY}" ]`,
  },
];

const PREFIX = 'missing ';

/** The script that prints one `missing <name>` line for each prerequisite the host lacks. */
export const PREREQUISITE_SCRIPT = HOST_PREREQUISITES.map(
  ({name, has}) => `{ ${has}; } || printf '${PREFIX}${name}\\n'`,
).join('; ');

/**
 * The prerequisites a host reported missing, in table order. A line that names nothing in the
 * table is not a report of the script and is ignored.
 */
export function missingPrerequisites(output: string): readonly string[] {
  const reported = new Set(
    output
      .split('\n')
      .map(line => line.trim())
      .filter(line => line.startsWith(PREFIX))
      .map(line => line.slice(PREFIX.length)),
  );
  return HOST_PREREQUISITES.map(({name}) => name).filter(name => reported.has(name));
}

/** The words of the refusal for a host that lacks `missing`. */
export const prerequisiteMessage = (missing: readonly string[]): string =>
  `the host lacks ${missing.join(', ')}, which installing the host program needs`;
