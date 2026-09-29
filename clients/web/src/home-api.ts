/**
 * The home server's setup API (sub-project 2). The wire types are copied from plan 2's "API
 * contract"; `src/entrypoints/web_home/contract.py` is their source of truth, so a contract change
 * updates this file in the same PR. A provider key leaves only in `saveKey`'s request body; no
 * response or error carries one back.
 */

export type ErrorCode =
  | 'unauthorized'
  | 'forbidden_origin'
  | 'not_found'
  | 'invalid_request'
  | 'internal_error'
  | 'invalid_path'
  | 'outside_roots'
  | 'permission_denied'
  | 'unknown_project'
  | 'not_git'
  | 'no_commits'
  | 'dirty_tree'
  | 'uninitialized'
  | 'no_tasks'
  | 'unknown_provider'
  | 'invalid_key'
  | 'symlink_rejected'
  | 'unknown_task'
  | 'task_invalid'
  | 'task_exists'
  | 'task_conflict'
  | 'task_read_only'
  | 'commit_failed'
  | 'unknown_run'
  | 'already_live'
  | 'launch_failed'
  | 'profile_guided_unavailable'
  | 'budget_decrease'
  | 'not_resumable';

interface FsEntry {
  name: string;
  /** Canonical. */
  path: string;
  git: boolean;
}

export interface FsListing {
  /** Null lists the granted roots. */
  path: string | null;
  parent: string | null;
  entries: FsEntry[];
}

type ProjectState =
  | 'missing'
  | 'not_git'
  | 'invalid'
  | 'uninitialized'
  | 'no_tasks'
  | 'no_commits'
  | 'dirty_tree'
  | 'ready';

interface ProjectRef {
  id: string;
  root: string;
  name: string;
}

export interface ProjectValidation {
  /** The first blocker in the listed order. */
  state: ProjectState;
  path: string;
  project: ProjectRef | null;
  message: string | null;
  tasks: string[];
  /** Changed and untracked paths relative to the project (only when it has commits). */
  pending: string[];
}

interface RecentProject extends ProjectRef {
  last_opened: string;
}

interface ProjectList {
  projects: RecentProject[];
  /** The user's home directory, for display only. */
  home: string | null;
}

export interface TaskSummary {
  name: string;
  valid: boolean;
  domain: string | null;
  error: string | null;
}

interface TaskList {
  tasks: TaskSummary[];
}

export type TaskDomain = 'llm-serving' | 'generic' | 'microservices' | 'database';

export interface TaskDetail {
  name: string;
  objective: string;
  domain: TaskDomain;
  accuracy_command: string;
  benchmark_command: string;
  result: {
    kind: 'metric' | 'protocol' | 'none';
    json_argument: string | null;
    metric: string | null;
    protocol_version: number | null;
  };
  profile_guided: boolean;
  editable: boolean;
  read_only_reason: string | null;
  content_hash: string;
}

export interface TaskForm {
  objective: string;
  domain: TaskDomain;
  accuracy_command: string;
  benchmark_command: string;
  result_json_argument: string;
  result_metric: string;
}

export interface TaskCreate extends TaskForm {
  name: string;
}

export interface TaskEdit extends TaskForm {
  base_hash: string;
}

export interface CommitPreview {
  task_files: string[];
  other: string[];
}

interface CommitResult {
  commit: string;
  committed: string[];
}

export type Driver = 'agentshim' | 'omnigent';
export type OuterLoopId = 'agent' | 'profile-guided' | 'dynamic' | 'plain' | 'evolve';
export type ComputeBackend = 'cuda' | 'metal' | 'trainium' | 'rocm' | 'cpu';

export interface OuterLoop {
  id: OuterLoopId;
  budget: {flag: '--max-rounds' | '--max-generations'; default: number};
  requires_profile_guided: boolean;
  roles: string[];
}

interface ProviderOption {
  provider: string;
  display_name: string;
  supports_reasoning_effort: boolean;
  suggested_models: string[];
}

export interface Catalog {
  drivers: {driver: Driver; providers: string[]; supports_docker: boolean}[];
  providers: ProviderOption[];
  outer_loops: OuterLoop[];
  compute_backends: ComputeBackend[];
  default_compute_backend: ComputeBackend;
}

interface ProviderKey {
  name: string;
  source: 'env' | 'dotenv' | 'missing';
  /** In the inherited environment (even empty) and in `.env`: the environment wins. */
  shadowed: boolean;
}

export interface ProviderAuth {
  provider: string;
  display_name: string;
  status: 'key' | 'cli_session' | 'missing';
  /** Empty for CLI-only providers. */
  keys: ProviderKey[];
  cli_session: 'present' | 'absent' | 'unknown';
  login_command: string;
}

export interface AuthStatus {
  dotenv_path: string;
  providers: ProviderAuth[];
}

interface KeyWriteResult {
  provider: string;
  name: string;
  status: 'unverified';
  shadowed_by_env: boolean;
}

export type GatewayState =
  | 'live'
  | 'starting'
  | 'ended_serving'
  | 'failed'
  | 'stale'
  | 'external'
  | 'reopened'
  | 'none';

export interface Gateway {
  state: GatewayState;
  url: string | null;
  /** Carries the gateway's own token (`...?token=`). */
  websocket_url: string | null;
  token: string | null;
  stderr_tail: string[];
  stderr_log: string | null;
  origin_mismatch: boolean;
}

export interface RunRow {
  run_id: string;
  loop: string | null;
  status: 'unknown' | 'active' | 'completed' | 'failed';
  rounds: number;
  gateway: Gateway;
  reopen: Gateway | null;
  /** The recorded total of the loop's budget flag; a resume cannot go below it. */
  budget: number | null;
  error: string | null;
  /** The task the run optimizes; null while a launch is not yet in the run store. */
  task: string | null;
  /** The run's effective objective; its first line titles the run. */
  objective: string | null;
  /** ISO 8601, from the run manifest. */
  created_at: string | null;
}

interface RunList {
  runs: RunRow[];
}

interface RoleOverride {
  model: string | null;
  reasoning_effort: string | null;
}

export interface StartRun {
  task: string;
  outer_loop: OuterLoopId;
  budget: number | null;
  compute_backend: ComputeBackend;
  driver: Driver | null;
  provider: string;
  model: string;
  reasoning_effort: string | null;
  roles: Record<string, RoleOverride>;
}

export interface LaunchResult {
  run_id: string;
  gateway: Gateway;
}

export class HomeError extends Error {
  constructor(
    readonly code: ErrorCode | 'network',
    message: string,
    readonly details: Readonly<Record<string, unknown>> | null,
  ) {
    super(message);
    this.name = 'HomeError';
  }
}

/** `fetch` as the client needs it: the page passes the browser's, tests pass a Fake. */
export type Fetch = (url: string, init: RequestInit) => Promise<Response>;

export interface HomeClient {
  fs(path: string | null): Promise<FsListing>;
  validate(path: string): Promise<ProjectValidation>;
  projects(): Promise<ProjectList>;
  tasks(projectId: string): Promise<TaskList>;
  task(projectId: string, name: string): Promise<TaskDetail>;
  createTask(projectId: string, body: TaskCreate): Promise<TaskDetail>;
  editTask(projectId: string, name: string, body: TaskEdit): Promise<TaskDetail>;
  commitPreview(projectId: string): Promise<CommitPreview>;
  commit(projectId: string, paths: readonly string[]): Promise<CommitResult>;
  catalog(): Promise<Catalog>;
  auth(): Promise<AuthStatus>;
  saveKey(provider: string, name: string, value: string): Promise<KeyWriteResult>;
  runs(projectId: string): Promise<RunList>;
  start(projectId: string, body: StartRun): Promise<LaunchResult>;
  open(projectId: string, runId: string): Promise<LaunchResult>;
  resume(projectId: string, runId: string, budget: number | null): Promise<LaunchResult>;
}

interface ErrorBody {
  error?: {code?: unknown; message?: unknown; details?: unknown};
}

function errorOf(status: number, payload: unknown): HomeError {
  const error = (payload as ErrorBody | null)?.error;
  if (typeof error?.code === 'string' && typeof error.message === 'string') {
    const details =
      typeof error.details === 'object' && error.details !== null
        ? (error.details as Record<string, unknown>)
        : null;
    return new HomeError(error.code as ErrorCode, error.message, details);
  }
  return new HomeError('internal_error', `The home server answered ${status}`, null);
}

const segment = encodeURIComponent;

export function homeClient(token: string, fetcher: Fetch): HomeClient {
  async function call<T>(method: 'GET' | 'POST' | 'PUT', path: string, body?: object): Promise<T> {
    const headers: Record<string, string> = {Authorization: `Bearer ${token}`};
    const init: RequestInit = {method, headers};
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let response: Response;
    try {
      response = await fetcher(path, init);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      throw new HomeError('network', `The home server did not answer (${reason})`, null);
    }
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) throw errorOf(response.status, payload);
    return payload as T;
  }
  const project = (id: string) => `/api/projects/${segment(id)}`;
  return {
    fs: path => call('GET', path === null ? '/api/fs' : `/api/fs?${new URLSearchParams({path})}`),
    validate: path => call('POST', '/api/projects/validate', {path}),
    projects: () => call('GET', '/api/projects'),
    tasks: id => call('GET', `${project(id)}/tasks`),
    task: (id, name) => call('GET', `${project(id)}/tasks/${segment(name)}`),
    createTask: (id, body) => call('POST', `${project(id)}/tasks`, body),
    editTask: (id, name, body) => call('PUT', `${project(id)}/tasks/${segment(name)}`, body),
    commitPreview: id => call('GET', `${project(id)}/commit`),
    commit: (id, paths) => call('POST', `${project(id)}/commit`, {paths, message: null}),
    catalog: () => call('GET', '/api/agents/catalog'),
    auth: () => call('GET', '/api/auth'),
    saveKey: (provider, name, value) =>
      call('PUT', `/api/auth/${segment(provider)}`, {name, value}),
    runs: id => call('GET', `${project(id)}/runs`),
    start: (id, body) => call('POST', `${project(id)}/runs`, body),
    open: (id, run) => call('POST', `${project(id)}/runs/${segment(run)}/open`, {}),
    resume: (id, run, budget) =>
      call('POST', `${project(id)}/runs/${segment(run)}/resume`, {budget}),
  };
}

const isText = (value: unknown): value is string => typeof value === 'string';

function detailLines(details: Readonly<Record<string, unknown>> | null): string[] {
  const tail = details?.['stderr_tail'];
  if (Array.isArray(tail)) return tail.filter(isText);
  const errors = details?.['errors'];
  if (!Array.isArray(errors)) return [];
  return errors
    .map(entry =>
      typeof entry === 'object' && entry !== null ? (entry as {msg?: unknown}).msg : null,
    )
    .filter(isText);
}

/** A failure as the user reads it: the message, then any stderr tail or validation messages. */
export function errorText(error: unknown): string {
  if (!(error instanceof HomeError)) return error instanceof Error ? error.message : String(error);
  return [error.message, ...detailLines(error.details)].join('\n');
}
