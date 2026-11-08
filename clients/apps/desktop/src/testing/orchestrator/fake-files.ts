/**
 * The file system a fake host serves to `ListDirectory` and `CheckRepository`: a small declared tree
 * answered as the real host answers, in its words and its status codes. Entries come sorted by name,
 * dot-prefixed ones only on request, and cut at the host's cap (`MAX_ENTRIES`); a folder is never
 * looked into below the listed one except for its own marks (a repository, its tasks).
 */
import {RpcStatus} from '../../core/errors.js';

/** The host's cap on one listing. */
export const MAX_ENTRIES = 300;

/** A folder of the fake tree. */
export interface FakeFolder {
  /** Holds a `.git`: a repository. */
  readonly repository?: boolean;
  /** The task folders under its `.vibesys/tasks`. */
  readonly tasks?: readonly string[];
  readonly folders?: Readonly<Record<string, FakeFolder>>;
  readonly files?: readonly string[];
}

/** What a host answers for a folder it cannot list. */
export interface FakeFault {
  readonly status: RpcStatus;
  readonly message: string;
}

export interface FakeEntry {
  readonly name: string;
  readonly isDirectory: boolean;
  readonly isGitRepository: boolean;
  readonly hasTasks: boolean;
}

export interface FakeListing {
  readonly entries: readonly FakeEntry[];
  readonly truncated: boolean;
}

/** A readable folder, a file, and a folder the daemon may not read, by what each answers. */
type Found =
  | {readonly kind: 'folder'; readonly folder: FakeFolder}
  | {readonly kind: 'file'}
  | {readonly kind: 'denied'}
  | {readonly kind: 'missing'};

export class FakeFiles {
  constructor(
    /** The home folder `~` stands for. */
    readonly home: string,
    readonly root: FakeFolder,
    /** Absolute paths of folders that exist and cannot be read. */
    readonly denied: readonly string[] = [],
  ) {}

  /** The absolute path a typed one names, cleaned; undefined for a relative one. */
  resolve(path: string): string | undefined {
    const absolute = path === '~' || path.startsWith('~/') ? `${this.home}${path.slice(1)}` : path;
    if (!absolute.startsWith('/')) return undefined;
    return `/${absolute
      .split('/')
      .filter(part => part !== '')
      .join('/')}`;
  }

  private find(absolute: string): Found {
    if (this.denied.includes(absolute)) return {kind: 'denied'};
    let folder = this.root;
    const parts = absolute.split('/').filter(part => part !== '');
    for (const [index, part] of parts.entries()) {
      const next = folder.folders?.[part];
      if (next !== undefined) {
        folder = next;
        continue;
      }
      return folder.files?.includes(part) === true && index === parts.length - 1
        ? {kind: 'file'}
        : {kind: 'missing'};
    }
    return {kind: 'folder', folder};
  }

  /** The listing of a folder, or the fault the host answers with. */
  list(path: string, hidden: boolean): FakeListing | FakeFault {
    const absolute = this.resolve(path);
    if (absolute === undefined)
      return {
        status: RpcStatus.INVALID_ARGUMENT,
        message: `path "${path}" is not absolute and does not start with ~/`,
      };
    const found = this.find(absolute);
    switch (found.kind) {
      case 'missing':
        return {status: RpcStatus.NOT_FOUND, message: `path ${absolute} does not exist`};
      case 'file':
        return {status: RpcStatus.INVALID_ARGUMENT, message: `path ${absolute} is not a directory`};
      case 'denied':
        return {
          status: RpcStatus.PERMISSION_DENIED,
          message: `path ${absolute} cannot be read: permission denied`,
        };
      case 'folder':
        break;
    }
    const children: FakeEntry[] = [
      ...Object.entries(found.folder.folders ?? {}).map(([name, child]) => ({
        name,
        isDirectory: true,
        isGitRepository: child.repository === true,
        hasTasks: (child.tasks ?? []).length > 0,
      })),
      ...(found.folder.files ?? []).map(name => ({
        name,
        isDirectory: false,
        isGitRepository: false,
        hasTasks: false,
      })),
      ...(found.folder.repository === true
        ? [{name: '.git', isDirectory: true, isGitRepository: false, hasTasks: false}]
        : []),
    ]
      .filter(entry => hidden || !entry.name.startsWith('.'))
      .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    return {entries: children.slice(0, MAX_ENTRIES), truncated: children.length > MAX_ENTRIES};
  }

  /** The host's words about a repository path, or undefined when a start could use it. */
  refusal(repository: string): string | undefined {
    const absolute = this.resolve(repository);
    if (absolute === undefined) return `repository "${repository}" is not an absolute path`;
    const found = this.find(absolute);
    if (found.kind !== 'folder') return `repository ${absolute} cannot be found`;
    return found.folder.repository === true
      ? undefined
      : `repository ${absolute} is not a git work tree: it has no .git`;
  }

  /** The tasks a repository holds, sorted. */
  tasksOf(repository: string): readonly string[] {
    const absolute = this.resolve(repository);
    const found = absolute === undefined ? undefined : this.find(absolute);
    return found?.kind === 'folder' ? [...(found.folder.tasks ?? [])].sort() : [];
  }
}

/** A believable home folder, for the fixture page. */
export const FIXTURE_FILES = new FakeFiles('/home/dev', {
  folders: {
    home: {
      folders: {
        dev: {
          folders: {
            code: {
              folders: {
                'decode-service': {repository: true, tasks: ['kv-cache', 'sampler']},
                'vision-kernels': {repository: true, tasks: ['fused-attention']},
                'serving-bench': {repository: true},
                'scratch-notes': {files: ['todo.md']},
                '.cache': {},
              },
              files: ['README.md'],
            },
            Downloads: {files: ['trace.json']},
            projects: {
              folders: {
                'llm-router': {repository: true, tasks: ['routing-latency']},
                'old-experiments': {},
              },
            },
          },
        },
      },
    },
  },
});
