/** Bounded OpenSSH Include traversal for desktop host discovery. */
import {open, opendir, realpath} from 'node:fs/promises';
import {homedir} from 'node:os';
import {isAbsolute, join, parse, resolve, sep} from 'node:path';
import {
  type SshConfigDirective,
  SshConfigSyntaxError,
  sshConfigDirectives,
  sshGlobSegmentMatches,
  sshHostPatternsMatch,
} from '@vibesys/settings';

const SSH_CONFIG_FILE_LIMIT = 256;
const SSH_CONFIG_DEPTH_LIMIT = 16;
const SSH_CONFIG_BYTE_LIMIT = 1024 * 1024;
const SSH_CONFIG_DIRECTORY_ENTRY_LIMIT = 16_384;
const SSH_CONFIG_INCLUDE_EVALUATION_LIMIT = 1024;

export type SshConfigDiscoveryErrorCode =
  | 'limit'
  | 'syntax'
  | 'unsupported-expansion'
  | 'unsupported-pattern';

/** A typed failure while interpreting the user's SSH configuration graph. */
export class SshConfigDiscoveryError extends Error {
  override readonly name = 'SshConfigDiscoveryError';

  constructor(
    readonly code: SshConfigDiscoveryErrorCode,
    message: string,
    readonly path: string,
    options: ErrorOptions = {},
  ) {
    super(message, options);
  }
}

/** Filesystem capabilities used to discover aliases across an OpenSSH configuration graph. */
export interface SshConfigFilesystem {
  /** Read at most `maximumBytes`; reject rather than returning a partial file. */
  readText(path: string, maximumBytes: number): Promise<string>;
  realPath(path: string): Promise<string>;
  /** Yield one directory entry name at a time so the caller can enforce its examination budget. */
  directoryEntries(path: string): AsyncIterable<string>;
}

const NODE_SSH_CONFIG_FILESYSTEM: SshConfigFilesystem = {
  readText: readBoundedText,
  realPath: path => realpath(path),
  directoryEntries: readDirectoryEntries,
};

export interface SshConfigDiscovery {
  readonly filesystem?: SshConfigFilesystem | undefined;
  /** Current user's home; injectable so tests never depend on the machine running them. */
  readonly homePath?: string | undefined;
}

/** Discover concrete aliases from a user config and its includes without opening a connection. */
export async function discoverSshConfigAliases(
  rootPath: string,
  options: SshConfigDiscovery = {},
): Promise<string[]> {
  const filesystem = options.filesystem ?? NODE_SSH_CONFIG_FILESYSTEM;
  const homePath = options.homePath ?? homedir();
  const walker = new SshConfigWalker(filesystem, join(homePath, '.ssh'), homePath);
  try {
    await walker.visit(rootPath, 0, [], new Set());
  } catch (error) {
    if (walker.rootUnread && isMissingFile(error)) return [];
    throw error;
  }
  return walker.result;
}

/** Null is an opaque Match context; arrays are conjunctions of original-alias pattern lists. */
type HostGate = readonly (readonly string[])[] | null;

class SshConfigWalker {
  readonly #parsedFiles = new Map<string, readonly SshConfigDirective[]>();
  readonly #aliases: string[] = [];
  readonly #aliasSet = new Set<string>();
  #bytes = 0;
  #directoryEntries = 0;
  #includeEvaluations = 0;
  #readFiles = 0;

  constructor(
    readonly filesystem: SshConfigFilesystem,
    readonly includeBase: string,
    readonly homePath: string,
  ) {}

  get rootUnread(): boolean {
    return this.#readFiles === 0;
  }

  get result(): string[] {
    return [...this.#aliases];
  }

  async visit(
    requestedPath: string,
    depth: number,
    inheritedGate: HostGate,
    ancestors: ReadonlySet<string>,
  ): Promise<void> {
    if (depth > SSH_CONFIG_DEPTH_LIMIT) {
      throw this.#limit(
        requestedPath,
        `SSH config includes exceed the ${SSH_CONFIG_DEPTH_LIMIT}-level traversal limit at ${requestedPath}`,
      );
    }
    const canonicalPath = await this.filesystem.realPath(requestedPath);
    if (ancestors.has(canonicalPath)) return;
    const directives = await this.#directives(canonicalPath);
    const nextAncestors = new Set(ancestors);
    nextAncestors.add(canonicalPath);
    let gate = inheritedGate;
    for (const directive of directives) {
      gate = await this.#accept(directive, depth, gate, inheritedGate, nextAncestors);
    }
  }

  async #accept(
    directive: SshConfigDirective,
    depth: number,
    gate: HostGate,
    inheritedGate: HostGate,
    ancestors: ReadonlySet<string>,
  ): Promise<HostGate> {
    if (directive.kind === 'host') {
      const hostGate = inheritedGate === null ? null : [...inheritedGate, directive.patterns];
      for (const alias of directive.aliases) {
        if (gateMatches(hostGate, alias)) this.#addAlias(alias);
      }
      return hostGate;
    }
    if (directive.kind === 'match') {
      return matchGate(directive.conditions, inheritedGate);
    }
    for (const pattern of directive.patterns) {
      await this.#include(pattern, directive.line, depth, gate, ancestors);
    }
    return gate;
  }

  async #directives(canonicalPath: string): Promise<readonly SshConfigDirective[]> {
    const cached = this.#parsedFiles.get(canonicalPath);
    if (cached !== undefined) return cached;
    if (this.#parsedFiles.size >= SSH_CONFIG_FILE_LIMIT) {
      throw this.#limit(
        canonicalPath,
        `SSH config includes exceed the ${SSH_CONFIG_FILE_LIMIT}-file traversal limit`,
      );
    }
    const text = await this.filesystem.readText(canonicalPath, SSH_CONFIG_BYTE_LIMIT - this.#bytes);
    this.#readFiles += 1;
    this.#bytes += Buffer.byteLength(text, 'utf8');
    if (this.#bytes > SSH_CONFIG_BYTE_LIMIT) {
      throw this.#limit(
        canonicalPath,
        `SSH config includes exceed the ${SSH_CONFIG_BYTE_LIMIT}-byte traversal limit`,
      );
    }
    let directives: readonly SshConfigDirective[];
    try {
      directives = sshConfigDirectives(text);
    } catch (error) {
      if (error instanceof SshConfigSyntaxError) {
        throw new SshConfigDiscoveryError(
          'syntax',
          `${canonicalPath}:${error.line}: ${error.message}`,
          canonicalPath,
          {cause: error},
        );
      }
      throw error;
    }
    this.#parsedFiles.set(canonicalPath, directives);
    return directives;
  }

  async #include(
    pattern: string,
    line: number,
    depth: number,
    gate: HostGate,
    ancestors: ReadonlySet<string>,
  ): Promise<void> {
    const resolved = resolveIncludePattern(pattern, this.includeBase, this.homePath, line);
    const matches = await this.#matchingPaths(resolved);
    for (const path of matches) {
      this.#includeEvaluations += 1;
      if (this.#includeEvaluations > SSH_CONFIG_INCLUDE_EVALUATION_LIMIT) {
        throw this.#limit(
          path,
          `SSH config includes exceed the ${SSH_CONFIG_INCLUDE_EVALUATION_LIMIT}-evaluation traversal limit`,
        );
      }
      await this.visit(path, depth + 1, gate, ancestors);
    }
  }

  async #matchingPaths(absolutePattern: string): Promise<readonly string[]> {
    const root = parse(absolutePattern).root;
    const segments = absolutePattern
      .slice(root.length)
      .split(sep)
      .filter(segment => segment !== '');
    let candidates = [root];
    for (const [index, segment] of segments.entries()) {
      if (!hasGlobCharacter(segment) && index < segments.length - 1) {
        candidates = candidates.map(path => join(path, segment));
        continue;
      }
      const next: string[] = [];
      for (const path of candidates) next.push(...(await this.#matchingChildren(path, segment)));
      candidates = [...new Set(next)].sort(codePointOrder);
      if (candidates.length === 0) break;
    }
    return candidates;
  }

  async #matchingChildren(directory: string, segment: string): Promise<readonly string[]> {
    const matches: string[] = [];
    try {
      for await (const entry of this.filesystem.directoryEntries(directory)) {
        this.#directoryEntries += 1;
        if (this.#directoryEntries > SSH_CONFIG_DIRECTORY_ENTRY_LIMIT) {
          throw this.#limit(
            directory,
            `SSH config includes exceed the ${SSH_CONFIG_DIRECTORY_ENTRY_LIMIT}-entry traversal limit`,
          );
        }
        if (sshGlobSegmentMatches(segment, entry)) matches.push(join(directory, entry));
      }
    } catch (error) {
      if (isMissingDirectory(error)) return [];
      throw error;
    }
    return matches;
  }

  #addAlias(alias: string): void {
    if (this.#aliasSet.has(alias)) return;
    this.#aliasSet.add(alias);
    this.#aliases.push(alias);
  }

  #limit(path: string, message: string): SshConfigDiscoveryError {
    return new SshConfigDiscoveryError('limit', message, path);
  }
}

function hasGlobCharacter(segment: string): boolean {
  return /[*?[]/.test(segment);
}

function gateMatches(gate: HostGate, alias: string): boolean {
  return gate?.every(patterns => sshHostPatternsMatch(patterns, alias)) ?? false;
}

/**
 * Evaluate only Match predicates whose result depends solely on the original candidate alias.
 * `host` uses the effective HostName after connection-time processing, which discovery does not
 * own; every mixed or otherwise stateful predicate therefore remains opaque too.
 */
function matchGate(conditions: readonly string[], inheritedGate: HostGate): HostGate {
  if (conditions.length === 1 && conditions[0]?.toLowerCase() === 'all') return inheritedGate;
  if (conditions.length !== 2 || conditions[0]?.toLowerCase() !== 'originalhost') return null;
  const patterns = conditions[1]?.split(',').filter(pattern => pattern !== '') ?? [];
  if (patterns.length === 0) return null;
  return inheritedGate === null ? null : [...inheritedGate, patterns];
}

function resolveIncludePattern(
  pattern: string,
  includeBase: string,
  homePath: string,
  line: number,
): string {
  if (pattern.includes('%') || pattern.includes('${')) {
    throw new SshConfigDiscoveryError(
      'unsupported-expansion',
      `SSH config line ${line}: Include expansion tokens are not supported: ${pattern}`,
      pattern,
    );
  }
  if (/\[\[(?:[:.=])/.test(pattern)) {
    throw new SshConfigDiscoveryError(
      'unsupported-pattern',
      `SSH config line ${line}: locale-dependent Include patterns are not supported: ${pattern}`,
      pattern,
    );
  }
  if (pattern.startsWith('~') && pattern !== '~' && !pattern.startsWith('~/')) {
    throw new SshConfigDiscoveryError(
      'unsupported-expansion',
      `SSH config line ${line}: Include for another user's home is not supported: ${pattern}`,
      pattern,
    );
  }
  if (pattern === '~') return homePath;
  if (pattern.startsWith('~/')) return join(homePath, pattern.slice(2));
  if (isAbsolute(pattern)) return pattern;
  return resolve(includeBase, pattern);
}

function codePointOrder(left: string, right: string): number {
  if (left < right) return -1;
  if (left > right) return 1;
  return 0;
}

function isMissingFile(error: unknown): boolean {
  return errorCode(error) === 'ENOENT';
}

function isMissingDirectory(error: unknown): boolean {
  const code = errorCode(error);
  return code === 'ENOENT' || code === 'ENOTDIR';
}

function errorCode(error: unknown): unknown {
  return typeof error === 'object' && error !== null && 'code' in error
    ? (error as {readonly code?: unknown}).code
    : undefined;
}

async function* readDirectoryEntries(path: string): AsyncIterable<string> {
  const directory = await opendir(path);
  try {
    while (true) {
      const entry = await directory.read();
      if (entry === null) return;
      yield entry.name;
    }
  } finally {
    await directory.close();
  }
}

async function readBoundedText(path: string, maximumBytes: number): Promise<string> {
  const file = await open(path, 'r');
  try {
    const chunks: Buffer[] = [];
    let bytes = 0;
    while (bytes <= maximumBytes) {
      const chunk = Buffer.allocUnsafe(Math.min(64 * 1024, maximumBytes + 1 - bytes));
      const result = await file.read(chunk, 0, chunk.length, null);
      if (result.bytesRead === 0) return Buffer.concat(chunks, bytes).toString('utf8');
      chunks.push(chunk.subarray(0, result.bytesRead));
      bytes += result.bytesRead;
    }
    throw new SshConfigDiscoveryError(
      'limit',
      `SSH config includes exceed the ${SSH_CONFIG_BYTE_LIMIT}-byte traversal limit`,
      path,
    );
  } finally {
    await file.close();
  }
}
