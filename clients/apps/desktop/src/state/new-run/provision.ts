/** Where a repository being made from an example stands: the fixed status slot reads it. */
export type ProvisionProgress =
  | {readonly _tag: 'Idle'}
  | {readonly _tag: 'Working'}
  | {readonly _tag: 'Done'; readonly repository: string}
  /** The host refused: its typed reason (the enum's name), its detail, and the repository in the way. */
  | {
      readonly _tag: 'Refused';
      readonly reason: string;
      readonly detail: string;
      readonly existing: string;
    }
  /** The call itself failed: no answer from the host. */
  | {readonly _tag: 'Failed'; readonly detail: string};

export const NOT_PROVISIONING: ProvisionProgress = {_tag: 'Idle'};

/** The repository name an example is copied to unless the person names another: its folder name. */
export const defaultName = (folder: string): string =>
  folder.toLowerCase().replace(/[^a-z0-9-]+/g, '-');

/** What the person typed for the parent folder, trimmed; empty means the host's own place. */
export const parentOf = (typed: string): string => typed.trim();
