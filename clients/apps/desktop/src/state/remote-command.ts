/**
 * The one shell text the app sends to a host over ssh: a command carried through any login shell
 * unchanged. ssh hands the command line to the account's login shell (sh, bash, zsh, tcsh, fish),
 * so the line is `sh -c '<fixed one-line script>' vibesys-command x<hex of the command>`. The
 * script contains no single quote, `!`, newline or backslash pair, so every one of those shells
 * passes it to `sh` unchanged, and the command is hex-encoded, which no shell alters; the script
 * decodes it back into `"$1"` and runs it with `/bin/sh -c`.
 */

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

const ROLE = 'vibesys-command';

/** `word` as a token every shell leaves alone: `x` and its UTF-8 bytes in hex. */
function hex(word: string): string {
  // `encodeURIComponent` yields the UTF-8 bytes as `%XX`, leaving unreserved ASCII as itself.
  const digits = encodeURIComponent(word).replace(
    /%([0-9A-F]{2})|[^%]/g,
    (match, pair: string | undefined) =>
      pair === undefined
        ? (match.codePointAt(0) ?? 0).toString(16).padStart(2, '0')
        : pair.toLowerCase(),
  );
  return `x${digits}`;
}

/** The remote command line that runs `script` with `/bin/sh -c` on the host. */
export function remoteCommand(script: string): string {
  if (script.includes('\0')) throw new Error('a remote command contains a NUL');
  return ['sh', '-c', `'${DECODE} exec /bin/sh -c "$1"'`, ROLE, hex(script)].join(' ');
}

/** The command a remote command line carries, read back from its hex; null for any other line. */
export function commandOf(line: string): string | null {
  const match = /^sh -c '([^']+)' vibesys-command x([a-f0-9]*)$/.exec(line);
  if (match === null || match[2] === undefined) return null;
  const digits = match[2];
  if (digits.length % 2 !== 0) return null;
  try {
    return decodeURIComponent(digits.replace(/../g, pair => `%${pair}`));
  } catch {
    return null;
  }
}
