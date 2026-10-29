/**
 * The one command the app runs on a host: the bootstrap that starts the host helper from a VibeSys
 * checkout (`docs/contributing/desktop-connection.md`, "Bootstrap"). Every action after it is a
 * typed message on the helper's channel; this is the only shell text the app sends to a host.
 *
 * The bootstrap checks the checkout and finds `uv`, then `exec`s
 * `uv run --no-sync --project CHECKOUT python -I -m entrypoints.host_helper`. It exits before
 * starting anything with a reserved status and one line on stdout when it cannot:
 *
 * - 64 `predates VERSION`: the checkout is VibeSys but older than the helper.
 * - 65 `nodir` | `nopyproject` | `notvibesys`: the path is not a VibeSys checkout.
 * - 66 `missing`: `uv` is on no PATH this account has, nor in the usual places.
 *
 * A status means that only together with its line, and only before the helper answered `hello`
 * (`bootstrapProblem`): a helper that later exits 65 is not a bootstrap report. Finding `uv` can
 * start a login shell, which is slow, so a link remembers the `uv` a bootstrap found (it prints
 * `uv PATH` before `exec`, `bootstrapUv`) and passes it to the next one, which then skips the probe
 * while that path still runs.
 *
 * It must mean the same under any login shell (sh, bash, zsh, tcsh, fish), since ssh hands the
 * command line to that shell. It is `sh -c '<fixed one-line script>' <role> <data>...`: the script
 * contains no single quote, `!`, newline, or backslash pair, so every one of those shells passes it
 * to `sh` unchanged, and each datum is hex-encoded (`x6869`), which no shell alters; the script
 * decodes them back into `"$@"`. Locally the same script runs as a direct spawn of `/bin/sh` with an
 * argument vector, so This Mac and an ssh host start the helper the same way.
 */

import type {CheckoutProblem} from '../state/host-connection.js';

/** Directories probed for `uv` when it is on neither the host's PATH nor a login shell's. */
const PROBED_DIRECTORIES = [
  '$HOME/.local/bin',
  '$HOME/.cargo/bin',
  '/opt/homebrew/bin',
  '/usr/local/bin',
] as const;

/** The bootstrap's reserved exit statuses, and what each means. */
const BOOTSTRAP_STATUS = {predates: 64, notCheckout: 65, noUv: 66} as const;

/**
 * Decodes the hex-encoded arguments back into `"$@"`. `\134` (a backslash) is the one backslash in
 * the script: a backslash before a digit is literal inside single quotes in every login shell.
 */
// Lines with shell `${...}` expansions are template literals that escape them as `\${`.
const DECODE = [
  'b=$(printf "\\134");',
  `d() { h=\${1#x}; r=; while [ -n "$h" ]; do t=\${h#??}; p=\${h%"$t"}; v=$((0x$p));`,
  `r="$r\${b}0$((v / 64))$((v / 8 % 8))$((v % 8))"; h=$t; done; printf %b "$r"; };`,
  'n=$#; while [ "$n" -gt 0 ]; do set -- "$@" "$(d "$1")"; shift; n=$((n - 1)); done;',
].join(' ');

const {predates, notCheckout, noUv} = BOOTSTRAP_STATUS;

/** The line a bootstrap prints, before `exec`, with the `uv` it found by probing. */
const UV_LINE = 'uv ';

/**
 * Check checkout `$1` (`~` and `~/` mean the home directory), find `uv` (`$2` when it is one a
 * bootstrap found before and it still runs), and start the helper.
 */
const BOOTSTRAP_SCRIPT = [
  DECODE,
  `p=$1; case $p in "~") p=$HOME ;; "~/"*) p="$HOME/\${p#??}" ;; esac;`,
  `[ -d "$p" ] || { echo nodir; exit ${notCheckout}; };`,
  `[ -f "$p/pyproject.toml" ] || { echo nopyproject; exit ${notCheckout}; };`,
  's=$(awk "/^[[]project[]]/ {s=1; next} /^[[]/ {s=0} s" "$p/pyproject.toml");',
  `printf "%s" "$s" | grep -q "^name *= *.vibesys.[ ]*$" || { echo notvibesys; exit ${notCheckout}; };`,
  'v=$(printf "%s" "$s" | sed -n "s/^version *= *.\\([^\\"]*\\).*/\\1/p" | head -n 1);',
  `[ -f "$p/src/entrypoints/host_helper.py" ] || { printf "predates %s" "$v"; echo; exit ${predates}; };`,
  'u=$2; [ -x "$u" ] || u=;',
  'if [ -z "$u" ]; then u=$(command -v uv 2>/dev/null) || u=; fi;',
  `if [ -z "$u" ]; then u=$("\${SHELL:-/bin/sh}" -l -c "command -v uv" </dev/null 2>/dev/null | tail -n 1); [ -x "$u" ] || u=; fi;`,
  `if [ -z "$u" ]; then for x in ${PROBED_DIRECTORIES.map(d => `"${d}"`).join(' ')}; do if [ -x "$x/uv" ]; then u=$x/uv; break; fi; done; fi;`,
  `if [ -z "$u" ]; then echo missing; exit ${noUv}; fi;`,
  `if [ "$u" = "$2" ]; then :; else printf "${UV_LINE}%s" "$u"; echo; fi;`,
  'exec "$u" run --no-sync --project "$p" python -I -m entrypoints.host_helper',
].join(' ');

const ROLE = 'vibesys-helper';

/** `word` as a token every shell leaves alone: `x` and its UTF-8 bytes in hex. */
function hex(word: string): string {
  return `x${Buffer.from(word, 'utf8').toString('hex')}`;
}

/** Refuse a path that cannot cross a command line intact (validated settings never hold one). */
function datum(path: string): string {
  if (/[\n\r\0]/.test(path)) {
    throw new Error(`a path contains a line break: ${JSON.stringify(path)}`);
  }
  return hex(path);
}

/**
 * The remote command line of an ssh exec channel that starts the helper from `checkout`, with the
 * `uv` an earlier bootstrap on the host found (null: probe for it).
 */
export function remoteBootstrap(checkout: string, uv: string | null): string {
  return ['sh', '-c', `'${BOOTSTRAP_SCRIPT}'`, ROLE, datum(checkout), datum(uv ?? '')].join(' ');
}

/** Carry a POSIX command through any login shell using the bootstrap's existing hex codec. */
export function remoteCommand(script: string): string {
  if (script.includes('\0')) throw new Error('a remote command contains a NUL');
  return ['sh', '-c', `'${DECODE} exec /bin/sh -c "$1"'`, 'vibesys-command', hex(script)].join(' ');
}

/** The local argument vector (for `/bin/sh`) that starts the helper from `checkout`. */
export function localBootstrap(checkout: string, uv: string | null): readonly string[] {
  return ['-c', BOOTSTRAP_SCRIPT, ROLE, datum(checkout), datum(uv ?? '')];
}

/** The `uv` a bootstrap reported finding in `line`, or null when the line is not that report. */
export function bootstrapUv(line: string): string | null {
  return line.startsWith(UV_LINE) && line.length > UV_LINE.length
    ? line.slice(UV_LINE.length)
    : null;
}

/** The checkout problems a status 65 reports, by its line. */
const NOT_CHECKOUT: Readonly<Record<string, (where: string) => string>> = {
  nodir: where => `${where} is not a directory.`,
  nopyproject: where =>
    `${where} has no pyproject.toml; pick the directory you cloned VibeSys into.`,
  notvibesys: where =>
    `${where} has a pyproject.toml, but it does not declare the vibesys project.`,
};

/**
 * What a bootstrap that exited before its helper answered `hello` means, for the user: the checkout
 * at `where` (`PATH on HOST`), from its exit `status` and the `lines` it printed, and which
 * checkout problem it is (`missing`: no VibeSys checkout at the path; `too-old`: one that predates
 * the helper; null: the checkout is fine and something else is missing). Null unless the status is
 * a reserved one and its own line is among `lines`: another program that happens to exit with a
 * reserved status is not a bootstrap report.
 */
export function bootstrapProblem(
  status: number | null,
  lines: readonly string[],
  where: string,
): {readonly message: string; readonly checkout: CheckoutProblem | null} | null {
  const said = lines.map(line => line.trim());
  switch (status) {
    case predates: {
      const line = said.find(text => text === 'predates' || text.startsWith('predates '));
      if (line === undefined) return null;
      const version = line.slice('predates'.length).trim() || 'unknown';
      return {
        message: `The VibeSys checkout ${where} predates the desktop helper (version ${version}). Update it, then try again.`,
        checkout: 'too-old',
      };
    }
    case notCheckout: {
      const line = said.find(text => Object.hasOwn(NOT_CHECKOUT, text));
      const message = line === undefined ? undefined : NOT_CHECKOUT[line]?.(where);
      return message === undefined ? null : {message, checkout: 'missing'};
    }
    case noUv:
      if (!said.includes('missing')) return null;
      return {
        checkout: null,
        message:
          `uv was not found ${where.slice(where.indexOf(' on ') + 1)}; tried the PATH of a ` +
          `non-interactive shell, the login shell's PATH, and ${PROBED_DIRECTORIES.map(d => `${d}/uv`).join(', ')}. ` +
          'Install uv there (https://docs.astral.sh/uv/) or put it on the PATH.',
      };
    default:
      return null;
  }
}
