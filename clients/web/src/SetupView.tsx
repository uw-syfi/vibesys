/** New run: loads the catalog, key status and recent folders; checks the folder; starts the run. */
import {type Dispatch, type SetStateAction, useCallback, useEffect, useRef, useState} from 'react';
import {
  type AuthStatus,
  type Catalog,
  type CommitPreview,
  errorText,
  type FsListing,
  type HomeClient,
  HomeError,
  type ProjectValidation,
  type TaskDetail,
  type TaskSummary,
} from './home-api.js';
import {type LaunchFailure, launchLine, useLaunch} from './launch.js';
import {homeHref} from './route.js';
import {
  blockers,
  draftOf,
  type FieldId,
  fieldId,
  folderStatus,
  initialForm,
  type KeyWrite,
  keyView,
  NEW_TASK,
  type RoleChoice,
  type SetupForm,
  saveError,
  startRequest,
  taskForm,
  withLoop,
  withoutDraft,
  withProvider,
  withTask,
  withTasks,
} from './setup.js';
import {CommitDialog} from './ui/CommitDialog.js';
import {FolderPicker} from './ui/FolderPicker.js';
import {KeyRow} from './ui/KeyRow.js';
import {
  Advanced,
  BudgetRow,
  EFFORTS,
  FolderRow,
  ModelRow,
  Roles,
  SetupFooter,
  TaskRow,
} from './ui/Setup.js';
import {StartFailure} from './ui/StartFailure.js';
import {TaskFormRows} from './ui/TaskForm.js';
import {Titlebar} from './ui/TitleRow.js';

interface SetupData {
  catalog: Catalog;
  auth: AuthStatus;
  /** Recent project roots, most recent first. */
  recent: string[];
}

function useSetupData(client: HomeClient): {
  data: SetupData | null;
  error: string | null;
  refreshAuth: () => void;
} {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [recent, setRecent] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fail = useCallback((reason: unknown) => setError(errorText(reason)), []);
  const refreshAuth = useCallback(() => void client.auth().then(setAuth, fail), [client, fail]);
  useEffect(() => {
    client.catalog().then(setCatalog, fail);
    client.projects().then(
      list => setRecent(list.projects.map(project => project.root)),
      () => setRecent([]),
    );
    refreshAuth();
  }, [client, fail, refreshAuth]);
  const data =
    catalog === null || auth === null || recent === null ? null : {catalog, auth, recent};
  return {data, error, refreshAuth};
}

interface Folder {
  validation: ProjectValidation | null;
  checking: boolean;
  error: string | null;
  check: (path: string) => void;
  recheck: () => void;
  /** The path was edited: forget the last check and drop any answer still in flight. */
  clear: () => void;
}

/** Folder checks. Each check takes a ticket; an answer whose ticket is no longer current is dropped. */
function useFolder(
  client: HomeClient,
  initial: string,
  onChecked: (validation: ProjectValidation) => void,
): Folder {
  const [validation, setValidation] = useState<ProjectValidation | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ticket = useRef(0);
  const checked = useRef<string | null>(null);
  const run = useCallback(
    (path: string) => {
      ticket.current += 1;
      const mine = ticket.current;
      checked.current = path;
      setChecking(true);
      setError(null);
      client.validate(path).then(
        result => {
          if (mine !== ticket.current) return;
          checked.current = result.path;
          setValidation(result);
          setChecking(false);
          onChecked(result);
        },
        (reason: unknown) => {
          if (mine !== ticket.current) return;
          setValidation(null);
          setChecking(false);
          setError(errorText(reason));
        },
      );
    },
    [client, onChecked],
  );
  const check = useCallback(
    (path: string) => {
      const trimmed = path.trim();
      if (trimmed !== '' && trimmed !== checked.current) run(trimmed);
    },
    [run],
  );
  const recheck = useCallback(() => {
    if (checked.current !== null) run(checked.current);
  }, [run]);
  const clear = useCallback(() => {
    ticket.current += 1;
    checked.current = null;
    setValidation(null);
    setChecking(false);
    setError(null);
  }, []);
  useEffect(() => check(initial), [check, initial]);
  return {validation, checking, error, check, recheck, clear};
}

/**
 * The checked project's tasks; null until they are known. Refetched after every check; the form
 * then keeps a valid choice or takes the first valid task.
 */
function useTasks(
  client: HomeClient,
  validation: ProjectValidation | null,
  setForm: Dispatch<SetStateAction<SetupForm>>,
): TaskSummary[] | null {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  useEffect(() => {
    if (tasks !== null) setForm(current => withTasks(current, tasks));
  }, [tasks, setForm]);
  useEffect(() => {
    let current = true;
    setTasks(null);
    const projectId = validation?.project?.id;
    if (projectId !== undefined) {
      client.tasks(projectId).then(
        list => {
          if (current) setTasks(list.tasks);
        },
        () => {
          if (current) setTasks([]);
        },
      );
    }
    return () => {
      current = false;
    };
  }, [client, validation]);
  return tasks;
}

function useTaskDetail(
  client: HomeClient,
  projectId: string | null,
  task: string | null,
): [TaskDetail | null, (detail: TaskDetail) => void] {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  useEffect(() => {
    let current = true;
    setDetail(null);
    if (projectId !== null && task !== null && task !== NEW_TASK) {
      client.task(projectId, task).then(
        result => {
          if (current) setDetail(result);
        },
        () => {
          if (current) setDetail(null);
        },
      );
    }
    return () => {
      current = false;
    };
  }, [client, projectId, task]);
  return [detail, setDetail];
}

interface Derived {
  loop: Catalog['outer_loops'][number] | undefined;
  effort: boolean;
  shown: TaskDetail | null;
  checked: SetupForm;
  blocking: ReturnType<typeof blockers>;
}

interface DeriveInput {
  form: SetupForm;
  catalog: Catalog;
  auth: AuthStatus;
  folder: Folder;
  tasks: TaskSummary[] | null;
  detail: TaskDetail | null;
  project: ProjectValidation['project'];
  keyWrite: KeyWrite;
}

/** The loop, reasoning-effort support, the form with an unlisted task cleared, and its blockers. */
function derive(input: DeriveInput): Derived {
  const {form, catalog, auth, folder, tasks, detail, project, keyWrite} = input;
  const loop = catalog.outer_loops.find(option => option.id === form.loop);
  const effort =
    catalog.providers.find(option => option.provider === form.provider)
      ?.supports_reasoning_effort ?? false;
  const shown = project !== null && detail?.name === form.task ? detail : null;
  // A task chosen in another folder, or before this folder's tasks arrived, is not a choice here.
  const listed = form.task === NEW_TASK || (tasks?.some(task => task.name === form.task) ?? false);
  const checked = listed ? form : {...form, task: null};
  const blocking = blockers({
    form: checked,
    validation: folder.validation,
    tasks: tasks ?? [],
    detail: shown,
    catalog,
    auth,
    checking: folder.checking,
    keyWrite,
  });
  return {loop, effort, shown, checked, blocking};
}

function formActions(setForm: Dispatch<SetStateAction<SetupForm>>, catalog: Catalog) {
  return {
    patch: (next: Partial<SetupForm>) => setForm(form => ({...form, ...next})),
    provider: (provider: string) => setForm(form => withProvider(form, catalog, provider)),
    loop: (loopId: string) => setForm(form => withLoop(form, catalog, loopId)),
    role: (role: string, choice: RoleChoice) =>
      setForm(form => ({...form, roles: {...form.roles, [role]: choice}})),
  };
}

/** A blocker link: open the enclosing disclosure, then bring the field into view and focus it. */
function focusField(field: FieldId): void {
  const element = document.getElementById(fieldId(field));
  element?.closest('details')?.setAttribute('open', '');
  element?.scrollIntoView({block: 'center'});
  element?.focus();
}

function NewRunTitle() {
  return (
    <Titlebar>
      <span className="name">New run</span>
    </Titlebar>
  );
}

interface StartFailedProps {
  failure: LaunchFailure;
  root: string | null;
  onRetry: () => void;
  onBack: () => void;
}

/** In place of the form while a launch is `failed`: the failure titlebar and `StartFailure`. */
function StartFailed({failure, root, onRetry, onBack}: StartFailedProps) {
  return (
    <>
      <Titlebar>
        <span className="name">New run</span>
        <span className="sp" />
        <span className="status">
          <span className="dot err" />
          Did not start
        </span>
      </Titlebar>
      <StartFailure
        failure={failure}
        root={root}
        backLabel="Back to setup"
        onRetry={onRetry}
        onBack={onBack}
      />
    </>
  );
}

interface ProviderKeyProps {
  client: HomeClient;
  auth: AuthStatus;
  provider: string;
  refreshAuth: () => void;
  /** Held by the form, whose footer names a rejected key; reset with the provider. */
  write: KeyWrite;
  setWrite: (write: KeyWrite) => void;
}

/**
 * The key typed for one provider. Mounted with `key={provider}`, so a provider change unmounts it
 * and the typed value goes with it; a successful write clears the value before anything else renders.
 */
function ProviderKey({client, auth, provider, refreshAuth, write, setWrite}: ProviderKeyProps) {
  const [value, setValue] = useState('');
  const row = auth.providers.find(item => item.provider === provider);
  if (row === undefined) return null;
  const name = row.keys[0]?.name;
  const save = () => {
    if (name === undefined || value === '') return;
    setWrite({kind: 'saving'});
    client.saveKey(provider, name, value).then(
      result => {
        setValue('');
        setWrite({kind: 'saved', shadowed: result.shadowed_by_env});
        refreshAuth();
      },
      (reason: unknown) => setWrite({kind: 'rejected', message: errorText(reason)}),
    );
  };
  return (
    <KeyRow
      view={keyView(row, write, auth.dotenv_path)}
      value={value}
      saving={write.kind === 'saving'}
      onValue={next => {
        setValue(next);
        if (write.kind !== 'idle' && write.kind !== 'saving') setWrite({kind: 'idle'});
      }}
      onSave={save}
      onRecheck={refreshAuth}
    />
  );
}

interface PickerState {
  listing: FsListing | null;
  error: string | null;
}

/** Each open takes a ticket; a listing or error whose ticket is no longer current is dropped. */
function usePicker(client: HomeClient) {
  const [picker, setPicker] = useState<PickerState | null>(null);
  const ticket = useRef(0);
  const open = useCallback(
    (path: string | null) => {
      const mine = ++ticket.current;
      setPicker(current => ({listing: current?.listing ?? null, error: null}));
      client.fs(path).then(
        listing => {
          if (mine !== ticket.current) return;
          setPicker(current => (current === null ? null : {listing, error: null}));
        },
        (reason: unknown) => {
          if (mine !== ticket.current) return;
          setPicker(current => (current === null ? null : {...current, error: errorText(reason)}));
        },
      );
    },
    [client],
  );
  const close = useCallback(() => setPicker(null), []);
  return {picker, open, close};
}

/** Adopts the chosen folder: forgets the last check, sets the path, and checks it again. */
function chooseFolder(
  picker: ReturnType<typeof usePicker>,
  folder: Folder,
  act: ReturnType<typeof formActions>,
  path: string,
): void {
  picker.close();
  folder.clear();
  act.patch({path});
  folder.check(path);
}

interface TaskSave {
  projectId: string;
  form: SetupForm;
  /** The content hash the edit started from; the server refuses the save if the task changed since. */
  base: string | null;
  onSaved: (detail: TaskDetail) => void;
  /** The task changed on disk: reload it so Discard shows what is there now. */
  onStale: () => void;
}

function useTaskSave(client: HomeClient) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The task changed on disk: Save stays off, and its message stays, until Discard or another task.
  const [conflict, setConflict] = useState(false);
  const save = ({projectId, form, base, onSaved, onStale}: TaskSave) => {
    const draft = form.draft;
    if (draft === null) return;
    setSaving(true);
    setError(null);
    const request =
      form.task === NEW_TASK
        ? client.createTask(projectId, {...taskForm(draft), name: draft.name})
        : client.editTask(projectId, form.task ?? '', {...taskForm(draft), base_hash: base ?? ''});
    request.then(
      saved => {
        setSaving(false);
        onSaved(saved);
      },
      (reason: unknown) => {
        setSaving(false);
        setError(saveError(reason));
        if (reason instanceof HomeError && reason.code === 'task_conflict') {
          setConflict(true);
          onStale();
        }
      },
    );
  };
  return {
    saving,
    error,
    conflict,
    save,
    /** A field was edited: a refused save's message goes, except a conflict's. */
    edited: () => {
      if (!conflict) setError(null);
    },
    /** The reload after a conflict failed. */
    staleFailed: (reason: unknown) =>
      setError(`The task changed on disk and could not be reloaded: ${errorText(reason)}`),
    reset: () => {
      setError(null);
      setConflict(false);
    },
  };
}

interface TaskFieldsProps {
  client: HomeClient;
  form: SetupForm;
  setForm: Dispatch<SetStateAction<SetupForm>>;
  tasks: TaskSummary[] | null;
  projectId: string | null;
  /** The folder is being checked. */
  checking: boolean;
  detail: TaskDetail | null;
  /** A saved or reloaded task. */
  onDetail: (detail: TaskDetail) => void;
  /** A save changed the task files: check the folder again. */
  onRecheck: () => void;
}

/** The task select or saved card, and the task form while creating or editing. */
function TaskFields({
  client,
  form,
  setForm,
  tasks,
  projectId,
  checking,
  detail,
  onDetail,
  onRecheck,
}: TaskFieldsProps) {
  const saver = useTaskSave(client);
  const onSaved = (saved: TaskDetail) => {
    onDetail(saved);
    setForm(current => ({...current, task: saved.name, draft: null}));
    onRecheck();
  };
  const onStale = () => {
    if (projectId !== null && form.task !== null)
      client.task(projectId, form.task).then(onDetail, saver.staleFailed);
  };
  // Taken when Edit opens the form, so a reload of the task after a conflict never becomes the base.
  const [base, setBase] = useState<string | null>(null);
  const draft = form.draft;
  return (
    <>
      <TaskRow
        form={form}
        tasks={tasks ?? []}
        detail={detail}
        disabled={projectId === null}
        checking={checking}
        onTask={name => {
          saver.reset();
          setForm(current => withTask(current, name));
        }}
        onEdit={() => {
          if (detail === null) return;
          saver.reset();
          setBase(detail.content_hash);
          setForm(current => ({...current, draft: draftOf(detail)}));
        }}
      />
      {draft === null ? null : (
        <TaskFormRows
          draft={draft}
          creating={form.task === NEW_TASK}
          saving={saver.saving}
          error={saver.error}
          conflict={saver.conflict}
          onDraft={next => {
            saver.edited();
            setForm(current =>
              current.draft === null ? current : {...current, draft: {...current.draft, ...next}},
            );
          }}
          onSave={() => {
            if (projectId !== null) saver.save({projectId, form, base, onSaved, onStale});
          }}
          onDiscard={() => {
            saver.reset();
            setForm(current => withoutDraft(current, tasks ?? []));
          }}
        />
      )}
    </>
  );
}

interface CommitState {
  preview: CommitPreview | null;
  error: string | null;
  busy: boolean;
}

/** The commit confirmation. Each preview takes a ticket; an answer for an older one is dropped. */
function useCommit(client: HomeClient, projectId: string | null, onDone: () => void) {
  const [state, setState] = useState<CommitState | null>(null);
  const ticket = useRef(0);
  const patch = (next: Partial<CommitState>) =>
    setState(current => (current === null ? null : {...current, ...next}));
  const load = (id: string, error: string | null) => {
    const mine = ++ticket.current;
    setState({preview: null, error, busy: false});
    client.commitPreview(id).then(
      preview => {
        if (mine === ticket.current) patch({preview});
      },
      (reason: unknown) => {
        if (mine === ticket.current) patch({error: errorText(reason)});
      },
    );
  };
  const confirm = () => {
    const preview = state?.preview;
    if (projectId === null || preview == null) return;
    patch({busy: true, error: null});
    // Cancel takes a new ticket: an answer after it neither reopens nor updates the dialog.
    const mine = ticket.current;
    client.commit(projectId, preview.task_files).then(
      () => {
        if (mine === ticket.current) setState(null);
        onDone();
      },
      (reason: unknown) => {
        if (mine !== ticket.current) return;
        // The files changed since the preview: show the new list, and the user confirms again.
        if (reason instanceof HomeError && reason.code === 'task_conflict') {
          load(projectId, 'The task files changed. Review the list again.');
        } else patch({busy: false, error: errorText(reason)});
      },
    );
  };
  const open = () => {
    if (projectId !== null) load(projectId, null);
  };
  const close = () => {
    ticket.current += 1;
    setState(null);
  };
  return {state, open, confirm, close};
}

interface SetupDialogsProps {
  folder: Folder;
  act: ReturnType<typeof formActions>;
  picker: ReturnType<typeof usePicker>;
  commit: ReturnType<typeof useCommit>;
}

/** The folder picker and the commit confirmation while open, and the reasoning effort suggestions. */
function SetupDialogs({folder, act, picker, commit}: SetupDialogsProps) {
  return (
    <>
      <datalist id="efforts">
        {EFFORTS.map(value => (
          <option key={value} value={value} />
        ))}
      </datalist>
      {picker.picker === null ? null : (
        <FolderPicker
          listing={picker.picker.listing}
          error={picker.picker.error}
          onOpen={picker.open}
          onChoose={path => chooseFolder(picker, folder, act, path)}
          onClose={picker.close}
        />
      )}
      {commit.state === null ? null : (
        <CommitDialog
          preview={commit.state.preview}
          error={commit.state.error}
          busy={commit.state.busy}
          onCommit={commit.confirm}
          onClose={commit.close}
        />
      )}
    </>
  );
}

/** One provider's key write, held by the form so its footer can name it; a provider change starts idle. */
function useKeyWrite(provider: string): [KeyWrite, (write: KeyWrite) => void] {
  const [held, setHeld] = useState<{provider: string; write: KeyWrite}>({
    provider,
    write: {kind: 'idle'},
  });
  const set = useCallback((write: KeyWrite) => setHeld({provider, write}), [provider]);
  return [held.provider === provider ? held.write : {kind: 'idle'}, set];
}

interface NewRunProps {
  client: HomeClient;
  token: string;
  data: SetupData;
  refreshAuth: () => void;
}

/** The form is inert (a disabled fieldset) while a launch is in flight: an edit then would be lost. */
function NewRun({client, token, data, refreshAuth}: NewRunProps) {
  const {catalog, auth, recent} = data;
  const [form, setForm] = useState(() => initialForm(catalog, auth, recent[0] ?? ''));
  const act = formActions(setForm, catalog);
  const onChecked = useCallback(
    (result: ProjectValidation) => setForm(current => ({...current, path: result.path})),
    [],
  );
  const folder = useFolder(client, recent[0] ?? '', onChecked);
  const project = folder.validation?.project ?? null;
  const tasks = useTasks(client, folder.validation, setForm);
  const [detail, setDetail] = useTaskDetail(client, project?.id ?? null, form.task);
  const launch = useLaunch(client, token);
  const picker = usePicker(client);
  const commit = useCommit(client, project?.id ?? null, folder.recheck);
  const [write, setWrite] = useKeyWrite(form.provider);
  const derived = derive({form, catalog, auth, folder, tasks, detail, project, keyWrite: write});
  const {loop, effort, shown, checked, blocking} = derived;
  const start = () => {
    if (project !== null)
      launch.start(project.id, () => client.start(project.id, startRequest(form, catalog)));
  };
  const line = launchLine(launch.state);
  if (launch.state.kind === 'failed')
    return (
      <StartFailed
        failure={launch.state.failure}
        root={project?.root ?? null}
        onRetry={start}
        onBack={launch.reset}
      />
    );
  return (
    <>
      <NewRunTitle />
      <div className="scroll">
        <fieldset className="form" disabled={line.busy !== null}>
          <FolderRow
            path={form.path}
            recent={recent}
            status={folderStatus(folder.validation, folder.checking, folder.error)}
            onPath={path => {
              folder.clear();
              act.patch({path});
            }}
            onCheck={() => folder.check(form.path)}
            onBrowse={() => picker.open(folder.validation?.path ?? null)}
            onCommit={commit.open}
          />
          <TaskFields
            client={client}
            form={checked}
            setForm={setForm}
            tasks={tasks}
            projectId={project?.id ?? null}
            checking={folder.checking}
            detail={shown}
            onDetail={setDetail}
            onRecheck={folder.recheck}
          />
          <BudgetRow loop={loop} value={form.budget} onBudget={budget => act.patch({budget})} />
          <ModelRow
            catalog={catalog}
            form={form}
            onProvider={act.provider}
            onModel={model => act.patch({model})}
          >
            <Roles roles={loop?.roles ?? []} form={form} effort={effort} onRole={act.role} />
          </ModelRow>
          <ProviderKey
            key={form.provider}
            client={client}
            auth={auth}
            provider={form.provider}
            refreshAuth={refreshAuth}
            write={write}
            setWrite={setWrite}
          />
          <Advanced
            catalog={catalog}
            form={form}
            effort={effort}
            onLoop={act.loop}
            onChange={act.patch}
          />
          <SetupDialogs folder={folder} act={act} picker={picker} commit={commit} />
        </fieldset>
      </div>
      <SetupFooter
        blockers={blocking}
        busy={line.busy}
        waiting={folder.checking || write.kind === 'saving'}
        error={line.error}
        cancelHref={homeHref(token, {kind: 'empty'})}
        onFix={focusField}
        onStart={start}
      />
    </>
  );
}

export interface SetupViewProps {
  client: HomeClient;
  token: string;
}

export function SetupView({client, token}: SetupViewProps) {
  const {data, error, refreshAuth} = useSetupData(client);
  if (error !== null) {
    return (
      <>
        <NewRunTitle />
        <p className="empty bad" role="alert">{`The home server did not answer: ${error}`}</p>
      </>
    );
  }
  if (data === null) {
    return (
      <>
        <NewRunTitle />
        <p className="empty">Loading…</p>
      </>
    );
  }
  return <NewRun client={client} token={token} data={data} refreshAuth={refreshAuth} />;
}
