/** New run form rows (mockup #setup). Rendering only: `SetupView` owns the state and the API calls. */
import {Check, ChevronDown, ChevronRight} from 'lucide-react';
import type {ReactNode, SelectHTMLAttributes} from 'react';
import {titleCase} from '../derive.js';
import type {Catalog, ComputeBackend, OuterLoop, TaskDetail, TaskSummary} from '../home-api.js';
import {
  type Blocker,
  budgetLabel,
  type FieldId,
  type FolderStatus,
  NEW_TASK,
  type RoleChoice,
  resultText,
  roleSummary,
  type SetupForm,
  suggestedModels,
} from '../setup.js';

type Tone = 'ok' | 'bad' | 'warn' | 'plain' | 'busy';

export const EFFORTS = ['low', 'medium', 'high'] as const;
const COMPUTE: Record<ComputeBackend, string> = {
  cuda: 'CUDA',
  metal: 'Metal',
  trainium: 'Trainium',
  rocm: 'ROCm',
  cpu: 'CPU',
};
const NO_CHOICE: RoleChoice = {model: '', effort: ''};

export function Row({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string | null;
  children: ReactNode;
}) {
  return (
    <div className="fr">
      {htmlFor === null ? (
        <span className="lab">{label}</span>
      ) : (
        <label htmlFor={htmlFor}>{label}</label>
      )}
      <div>{children}</div>
    </div>
  );
}

/**
 * A status line under a field; `ok` adds a check mark and keeps the text secondary, `busy` a spinner.
 * A `bad` hint is an alert, the result of an action; a hint with an `id` describes its field's state
 * (the field names it in `aria-describedby`) and is never an alert, so a load does not announce it.
 */
export function Hint({tone, id, children}: {tone: Tone; id?: string; children: ReactNode}) {
  const className = tone === 'bad' || tone === 'warn' ? `hint ${tone}` : 'hint';
  return (
    <div
      id={id}
      className={className}
      role={tone === 'bad' && id === undefined ? 'alert' : undefined}
    >
      {tone === 'ok' ? <Check size={12} strokeWidth={1.75} className="ok" aria-hidden /> : null}
      {tone === 'busy' ? <span className="spin" aria-hidden /> : null}
      {children}
    </div>
  );
}

/** A native select drawn as a field, with the form's own chevron. */
export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <span className="selbox">
      <select {...props} />
      <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
    </span>
  );
}

function Disclosure({summary, children}: {summary: string; children: ReactNode}) {
  return (
    <details className="opt">
      <summary className="disc">
        <ChevronRight size={14} strokeWidth={1.5} className="chev" aria-hidden />
        {summary}
      </summary>
      {children}
    </details>
  );
}

export interface FolderRowProps {
  path: string;
  recent: readonly string[];
  status: FolderStatus | null;
  onPath: (path: string) => void;
  onCheck: () => void;
  /** Opens the folder picker. */
  onBrowse?: () => void;
  /** Opens the commit confirmation. */
  onCommit?: () => void;
}

export function FolderRow({
  path,
  recent,
  status,
  onPath,
  onCheck,
  onBrowse,
  onCommit,
}: FolderRowProps) {
  const commit = status?.commit === true && onCommit !== undefined;
  return (
    <Row label="Folder" htmlFor="f-folder">
      <div className="row2">
        <input
          id="f-folder"
          className={status?.tone === 'bad' ? 'fld mono bad' : 'fld mono'}
          list="recent-folders"
          value={path}
          aria-describedby={status === null ? undefined : 'f-folder-hint'}
          placeholder="/path/to/a/git/repository"
          spellCheck={false}
          autoComplete="off"
          onChange={event => onPath(event.target.value)}
          onBlur={onCheck}
          onKeyDown={event => {
            if (event.key === 'Enter') onCheck();
          }}
        />
        <datalist id="recent-folders">
          {recent.map(root => (
            <option key={root} value={root} />
          ))}
        </datalist>
        {onBrowse === undefined ? null : (
          <button type="button" className="btn" onClick={onBrowse}>
            Browse…
          </button>
        )}
      </div>
      {status === null ? null : (
        <Hint tone={status.tone} id="f-folder-hint">
          {status.text}
          {commit ? (
            <button type="button" className="linkbtn" onClick={onCommit}>
              Commit task files…
            </button>
          ) : null}
        </Hint>
      )}
    </Row>
  );
}

export interface TaskRowProps {
  form: SetupForm;
  tasks: readonly TaskSummary[];
  detail: TaskDetail | null;
  /** No project yet: nothing to choose from. */
  disabled: boolean;
  /** The folder is being checked. */
  checking: boolean;
  onTask: (name: string) => void;
  /** Opens the saved task in the form. */
  onEdit?: () => void;
}

function emptyTaskText(disabled: boolean, checking: boolean): string {
  if (checking) return 'Checking the folder…';
  return disabled ? 'Choose a folder first' : 'Loading tasks…';
}

function TaskSelect({
  className,
  form,
  tasks,
  disabled,
  checking,
  onTask,
}: Omit<TaskRowProps, 'detail' | 'onEdit'> & {className: string}) {
  return (
    <Select
      id="f-task"
      className={className}
      value={form.task ?? ''}
      disabled={disabled}
      onChange={event => onTask(event.target.value)}
    >
      {form.task === null ? <option value="">{emptyTaskText(disabled, checking)}</option> : null}
      {tasks.map(task => (
        <option
          key={task.name}
          value={task.name}
          disabled={!task.valid}
          title={task.error ?? undefined}
        >
          {task.valid ? task.name : `${task.name} (invalid)`}
        </option>
      ))}
      <option value={NEW_TASK}>New task…</option>
    </Select>
  );
}

function TaskAction({detail, onEdit}: {detail: TaskDetail; onEdit: (() => void) | undefined}) {
  if (!detail.editable) {
    return (
      <span className="ro" title={detail.read_only_reason ?? undefined}>
        Read-only
      </span>
    );
  }
  if (onEdit === undefined) return null;
  return (
    <button type="button" className="btn" onClick={onEdit}>
      Edit
    </button>
  );
}

export function TaskRow({form, tasks, detail, disabled, checking, onTask, onEdit}: TaskRowProps) {
  const select = (className: string) => (
    <TaskSelect
      className={className}
      form={form}
      tasks={tasks}
      disabled={disabled}
      checking={checking}
      onTask={onTask}
    />
  );
  if (form.draft !== null || detail === null) {
    return (
      <Row label="Task" htmlFor="f-task">
        {select('fld')}
      </Row>
    );
  }
  return (
    <Row label="Task" htmlFor="f-task">
      <div className="summary">
        {select('t')}
        <span className="m obj" title={detail.objective}>
          {detail.objective}
        </span>
        <span className="m">
          <span className="mono" title={detail.benchmark_command}>
            {detail.benchmark_command}
          </span>
          , <span className="nw">{resultText(detail)}</span>
        </span>
        <TaskAction detail={detail} onEdit={onEdit} />
      </div>
    </Row>
  );
}

export function BudgetRow({
  loop,
  value,
  onBudget,
}: {
  loop: OuterLoop | undefined;
  value: string;
  onBudget: (value: string) => void;
}) {
  const label = budgetLabel(loop);
  return (
    <Row label={label} htmlFor="f-budget">
      <input
        id="f-budget"
        className="fld num budget"
        type="number"
        min={1}
        step={1}
        inputMode="numeric"
        value={value}
        title={`Total ${label.toLowerCase()} for this run (${loop?.budget.flag ?? '--max-rounds'})`}
        onChange={event => onBudget(event.target.value)}
      />
    </Row>
  );
}

export interface ModelRowProps {
  catalog: Catalog;
  form: SetupForm;
  onProvider: (provider: string) => void;
  onModel: (model: string) => void;
  /** Under the model field: the per-role disclosure. */
  children?: ReactNode;
}

export function ModelRow({catalog, form, onProvider, onModel, children}: ModelRowProps) {
  return (
    <Row label="Model" htmlFor="f-model">
      <div className="fld model">
        <select
          aria-label="Provider"
          value={form.provider}
          onChange={event => onProvider(event.target.value)}
        >
          {catalog.providers.map(option => (
            <option key={option.provider} value={option.provider}>
              {option.display_name}
            </option>
          ))}
        </select>
        <input
          id="f-model"
          list="models"
          value={form.model}
          placeholder="Model name"
          spellCheck={false}
          autoComplete="off"
          onChange={event => onModel(event.target.value)}
        />
        <ChevronDown size={14} strokeWidth={1.5} aria-hidden />
        <datalist id="models">
          {suggestedModels(catalog, form.provider).map(model => (
            <option key={model} value={model} />
          ))}
        </datalist>
      </div>
      {children}
    </Row>
  );
}

export interface RolesProps {
  roles: readonly string[];
  form: SetupForm;
  effort: boolean;
  onRole: (role: string, choice: RoleChoice) => void;
}

export function Roles({roles, form, effort, onRole}: RolesProps) {
  if (roles.length === 0) return null;
  return (
    <Disclosure summary={roleSummary(form, roles)}>
      {roles.map(role => {
        const choice = form.roles[role] ?? NO_CHOICE;
        const name = titleCase(role);
        return (
          <div key={role} className="role">
            <span>{name}</span>
            <input
              className="fld mono"
              list="models"
              aria-label={`${name} model`}
              placeholder={form.model || 'Model name'}
              value={choice.model}
              spellCheck={false}
              autoComplete="off"
              onChange={event => onRole(role, {...choice, model: event.target.value})}
            />
            {effort ? (
              <input
                className="fld effort"
                list="efforts"
                aria-label={`${name} reasoning effort`}
                placeholder="Reasoning"
                value={choice.effort}
                autoComplete="off"
                onChange={event => onRole(role, {...choice, effort: event.target.value})}
              />
            ) : null}
          </div>
        );
      })}
    </Disclosure>
  );
}

export interface AdvancedProps {
  catalog: Catalog;
  form: SetupForm;
  effort: boolean;
  onLoop: (loopId: string) => void;
  onChange: (patch: Partial<SetupForm>) => void;
}

export function Advanced({catalog, form, effort, onLoop, onChange}: AdvancedProps) {
  const drivers = catalog.drivers.filter(option => option.providers.includes(form.provider));
  return (
    <Disclosure summary="Advanced">
      <Row label="Outer loop" htmlFor="f-loop">
        <Select
          id="f-loop"
          className="fld"
          value={form.loop}
          onChange={event => onLoop(event.target.value)}
        >
          {catalog.outer_loops.map(loop => (
            <option key={loop.id} value={loop.id}>
              {titleCase(loop.id)}
            </option>
          ))}
        </Select>
      </Row>
      <Row label="Compute" htmlFor="f-compute">
        <Select
          id="f-compute"
          className="fld"
          value={form.compute}
          title="Where benchmarks run on this machine"
          onChange={event => {
            const compute = catalog.compute_backends.find(
              backend => backend === event.target.value,
            );
            if (compute !== undefined) onChange({compute});
          }}
        >
          {catalog.compute_backends.map(backend => (
            <option key={backend} value={backend}>
              {COMPUTE[backend]}
            </option>
          ))}
        </Select>
      </Row>
      <Row label="Agent driver" htmlFor="f-driver">
        <Select
          id="f-driver"
          className="fld"
          value={form.driver ?? ''}
          onChange={event =>
            onChange({
              driver: drivers.find(option => option.driver === event.target.value)?.driver ?? null,
            })
          }
        >
          <option value="">Default</option>
          {drivers.map(option => (
            <option key={option.driver} value={option.driver}>
              {titleCase(option.driver)}
            </option>
          ))}
        </Select>
      </Row>
      {effort ? (
        <Row label="Reasoning" htmlFor="f-effort">
          <input
            id="f-effort"
            className="fld"
            list="efforts"
            value={form.effort}
            placeholder="Provider default"
            autoComplete="off"
            onChange={event => onChange({effort: event.target.value})}
          />
        </Row>
      ) : null}
    </Disclosure>
  );
}

export interface SetupFooterProps {
  blockers: readonly Blocker[];
  /** What is in flight; disables Start. */
  busy: string | null;
  /** A check or key save is in flight: Start waits, and the field says so. */
  waiting: boolean;
  error: string | null;
  cancelHref: string;
  onFix: (field: FieldId) => void;
  onStart: () => void;
}

function FooterLine({
  blockers,
  busy,
  error,
  onFix,
}: Omit<SetupFooterProps, 'cancelHref' | 'onStart' | 'waiting'>) {
  if (busy !== null) {
    return (
      <span className="busy" aria-live="polite">
        <span className="spin" />
        {busy}
      </span>
    );
  }
  if (error !== null) {
    return (
      <span className="bad" role="alert">
        {error}
      </span>
    );
  }
  if (blockers.length === 0) return null;
  return (
    <>
      <span className="cnt">{`${blockers.length} to fix:`}</span>
      <span className="blk">
        {blockers.map(blocker => (
          <button
            key={`${blocker.field}:${blocker.text}`}
            type="button"
            className="linkbtn"
            onClick={() => onFix(blocker.field)}
          >
            {blocker.text}
          </button>
        ))}
      </span>
    </>
  );
}

export function SetupFooter({
  blockers,
  busy,
  waiting,
  error,
  cancelHref,
  onFix,
  onStart,
}: SetupFooterProps) {
  return (
    <footer className="sheetfoot">
      <FooterLine blockers={blockers} busy={busy} error={error} onFix={onFix} />
      <span className="sp" />
      <a className="btn ghost" href={cancelHref}>
        Cancel
      </a>
      <button
        type="button"
        className="btn primary"
        disabled={blockers.length > 0 || busy !== null || waiting}
        onClick={onStart}
      >
        Start run
      </button>
    </footer>
  );
}
