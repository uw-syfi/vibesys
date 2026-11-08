/**
 * The folder listings behind the Start form's repository path, as a shell completes a path: the
 * text typed so far is split at its last slash into the folder to list and the start of a name in
 * it. The host lists one folder at a time (`ListDirectory`); the client keeps each answer by folder
 * and narrows it by the typed start of a name, so typing within a folder asks nothing more. What an
 * entry is (a repository, one with tasks) is the host's word; the client only orders and filters.
 */

/** One child of a listed folder, as the host described it. */
export interface FolderEntry {
  readonly name: string;
  readonly isDirectory: boolean;
  readonly isGitRepository: boolean;
  readonly hasTasks: boolean;
}

/** Where the listing of one folder stands. */
export type FolderListing =
  | {readonly _tag: 'Loading'}
  | {
      readonly _tag: 'Listed';
      readonly entries: readonly FolderEntry[];
      /** The host cut the listing at its cap: later names are not here. */
      readonly truncated: boolean;
    }
  /** The host could not list the folder (it is not there, not readable); the client then offers nothing. */
  | {readonly _tag: 'Failed'; readonly reason: string};

/** The folder a typed path has reached and the start of a name inside it. */
export interface PathSplit {
  /** The typed text up to and including its last slash: what a completion is appended to. */
  readonly folder: string;
  /** What is typed after the last slash. */
  readonly prefix: string;
  /** Dot-prefixed entries are listed only when the name being typed starts with a dot. */
  readonly hidden: boolean;
}

/**
 * Split typed text at its last slash. Text with no slash names nothing to list (a relative path is
 * not one the host resolves), except a lone `~`, which is the home folder.
 */
export function splitPath(typed: string): PathSplit | undefined {
  const text = typed === '~' ? '~/' : typed;
  const slash = text.lastIndexOf('/');
  if (slash < 0 || !(text.startsWith('/') || text.startsWith('~/'))) return undefined;
  const prefix = text.slice(slash + 1);
  return {folder: text.slice(0, slash + 1), prefix, hidden: prefix.startsWith('.')};
}

/** The key one folder's listing is kept under. */
export const listingKey = (folder: string, hidden: boolean): string =>
  `${hidden ? 'hidden' : 'visible'}:${folder}`;

/** What the field offers: the completed text a pick puts in the field, and what the entry is. */
export interface PathCompletion {
  /** The whole path after a pick: a folder ends with a slash, so its own listing follows. */
  readonly value: string;
  /** A short word for what the entry is, or undefined for a plain one. */
  readonly detail: string | undefined;
}

const detailOf = (entry: FolderEntry): string | undefined => {
  if (entry.hasTasks) return 'tasks';
  return entry.isGitRepository ? 'git' : undefined;
};

/**
 * The completions of the typed text from the listing of its folder: the folders whose names start
 * with what follows the last slash, in the host's order, none until the folder is listed. A file
 * is never offered: the path is a repository, which is a folder.
 */
export function completionsOf(
  split: PathSplit,
  listing: FolderListing | undefined,
): readonly PathCompletion[] {
  if (listing?._tag !== 'Listed') return [];
  return listing.entries
    .filter(entry => entry.isDirectory && entry.name.startsWith(split.prefix))
    .map(entry => ({value: `${split.folder}${entry.name}/`, detail: detailOf(entry)}));
}
