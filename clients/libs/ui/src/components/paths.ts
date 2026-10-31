/**
 * Text a person reads never carries a filesystem path. A refusal or an error often names the file
 * or folder it came from, in full (a temporary folder, a home directory); `withoutPaths` reduces
 * each path in `text` to its last name, so "Could not read /var/tmp/x/hosts.json" reads "Could not
 * read hosts.json". A path is a drive path (`C:\x\y`), a home or relative one (`~/x`, `./x`,
 * `../x`), or an absolute one of two or more names (`/a/b`). A URL, a ratio ("4/5"), "and/or" and
 * a single `/command` are not paths and stay as written.
 */
const NAME = String.raw`[^\s/\\'"<>|:;,()\[\]{}]+`;
const PATH = new RegExp(
  String.raw`(?<![\w./~\\-])(?:[A-Za-z]:\\(?:${NAME}\\)*${NAME}|(?:~|\.{1,2})?(?:/${NAME})+)`,
  'g',
);

function isPath(token: string): boolean {
  if (/^[A-Za-z]:\\/.test(token) || /^(?:~|\.{1,2})\//.test(token)) return true;
  return token.split('/').length > 2;
}

export function withoutPaths(text: string): string {
  return text.replace(PATH, found => {
    if (!isPath(found)) return found;
    return (
      found
        .split(/[\\/]/)
        .filter(name => name !== '')
        .at(-1) ?? found
    );
  });
}
