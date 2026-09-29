/** New run: loads the catalog, key status and recent folders; checks the folder; starts the run. */
import {type Dispatch, type SetStateAction, useCallback, useEffect, useRef, useState} from 'react';
import {
  type AuthStatus,
  type Catalog,
  errorText,
  type FsListing,
  type HomeClient,
  type ProjectValidation,
  type TaskDetail,
  type TaskSummary,
} from './home-api.js';
import {launchLine, useLaunch} from './launch.js';
import {homeHref} from './route.js';
import {
  blockers,
  type FieldId,
  fieldId,
  folderStatus,
  initialForm,
  type KeyWrite,
  keyView,
  NEW_TASK,
  type RoleChoice,
  type SetupForm,
  startRequest,
  withLoop,
  withProvider,
  withTask,
  withTasks,
} from './setup.js';
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

/** The checked project's tasks; null until they are known. Refetched after every check. */
function useTasks(client: HomeClient, validation: ProjectValidation | null): TaskSummary[] | null {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
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

function formActions(setForm: Dispatch<SetStateAction<SetupForm>>, catalog: Catalog) {
  return {
    patch: (next: Partial<SetupForm>) => setForm(form => ({...form, ...next})),
    task: (name: string) => setForm(form => withTask(form, name)),
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

function Titlebar() {
  return (
    <header className="titlebar">
      <span className="name">New run</span>
    </header>
  );
}

interface ProviderKeyProps {
  client: HomeClient;
  auth: AuthStatus;
  provider: string;
  refreshAuth: () => void;
}

/**
 * The key typed for one provider. Mounted with `key={provider}`, so a provider change unmounts it
 * and the typed value goes with it; a successful write clears the value before anything else renders.
 */
function ProviderKey({client, auth, provider, refreshAuth}: ProviderKeyProps) {
  const [value, setValue] = useState('');
  const [write, setWrite] = useState<KeyWrite>({kind: 'idle'});
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

interface FolderPickerHostProps {
  folder: Folder;
  act: ReturnType<typeof formActions>;
  picker: ReturnType<typeof usePicker>;
}

/** The picker dialog, or nothing while it is closed. */
function FolderPickerHost({folder, act, picker}: FolderPickerHostProps) {
  if (picker.picker === null) return null;
  return (
    <FolderPicker
      listing={picker.picker.listing}
      error={picker.picker.error}
      onOpen={picker.open}
      onChoose={path => chooseFolder(picker, folder, act, path)}
      onClose={picker.close}
    />
  );
}

interface NewRunProps {
  client: HomeClient;
  token: string;
  data: SetupData;
  refreshAuth: () => void;
}

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
  const tasks = useTasks(client, folder.validation);
  const [detail] = useTaskDetail(client, project?.id ?? null, form.task);
  const launch = useLaunch(client, token);
  const picker = usePicker(client);
  useEffect(() => {
    if (tasks !== null) setForm(current => withTasks(current, tasks));
  }, [tasks]);
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
  });
  const start = () => {
    if (project !== null)
      launch.start(project.id, () => client.start(project.id, startRequest(form, catalog)));
  };
  const line = launchLine(launch.state);
  return (
    <>
      <Titlebar />
      <div className="scroll">
        <div className="form">
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
          />
          <TaskRow
            form={checked}
            tasks={tasks ?? []}
            detail={shown}
            disabled={project === null}
            onTask={act.task}
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
          />
          <Advanced
            catalog={catalog}
            form={form}
            effort={effort}
            onLoop={act.loop}
            onChange={act.patch}
          />
          <datalist id="efforts">
            {EFFORTS.map(value => (
              <option key={value} value={value} />
            ))}
          </datalist>
          <FolderPickerHost folder={folder} act={act} picker={picker} />
        </div>
      </div>
      <SetupFooter
        blockers={blocking}
        busy={line.busy}
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
        <Titlebar />
        <p className="empty bad" role="alert">{`The home server did not answer: ${error}`}</p>
      </>
    );
  }
  if (data === null) {
    return (
      <>
        <Titlebar />
        <p className="empty">Loading…</p>
      </>
    );
  }
  return <NewRun client={client} token={token} data={data} refreshAuth={refreshAuth} />;
}
