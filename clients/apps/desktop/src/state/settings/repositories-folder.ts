/**
 * The Repositories folder kept for a machine: where its new repositories are made. The person types
 * it as an absolute path or `~` (the host expands it); empty keeps none, so the machine's host
 * default applies. The rule is the contract's (`DesktopHostSettings.repositories_directory`): the
 * folder is checked here first so a bad one is refused beside the field, naming it.
 */

/** What the field is called on screen, and in every refusal. */
export const REPOSITORIES_FIELD = 'Repositories';

const MAX_LENGTH = 1024;
const ABSOLUTE_OR_HOME = /^(~$|\/|~\/)/;
const CONTROL = /[\0\r\n]/;

export type FolderCheck =
  | {
      readonly ok: true;
      /** The folder to keep; empty for none. */
      readonly directory: string;
    }
  | {readonly ok: false; readonly message: string};

/** The folder `input` stands for, or the refusal that names the field. */
export function checkRepositoriesFolder(input: string): FolderCheck {
  const directory = input.trim();
  if (directory === '') return {ok: true, directory: ''};
  if (!ABSOLUTE_OR_HOME.test(directory))
    return {ok: false, message: `${REPOSITORIES_FIELD}: absolute path or ~`};
  if (CONTROL.test(directory)) return {ok: false, message: `${REPOSITORIES_FIELD}: no line breaks`};
  if (directory.length > MAX_LENGTH) return {ok: false, message: `${REPOSITORIES_FIELD}: too long`};
  return {ok: true, directory};
}
