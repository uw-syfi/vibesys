const LOCAL_INSTANCE = '0123456789ab';
const SSH_INSTANCE = 'ba9876543210';
const CHROME = {mode: 'welcome', attached: null, stops: {}};
const HOSTS = {
  local: {
    label: 'This Mac',
    project: '/projects/local',
    task: 'queue',
    instance: LOCAL_INSTANCE,
    run: 'local-run',
    hostname: 'workstation.local',
    pid: 4242,
  },
  'ssh:gpu': {
    label: 'gpu',
    project: '/projects/remote',
    task: 'train',
    instance: SSH_INSTANCE,
    run: 'remote-run',
    hostname: 'gpu.internal',
    pid: 5252,
  },
};
const RECENT = Object.entries(HOSTS).map(([host, facts], index) => ({
  host,
  hostLabel: facts.label,
  project: facts.project,
  task: facts.task,
  instanceId: facts.instance,
  runId: facts.run,
  attachedAt: 2 - index,
}));

const control = {
  inspectionCalls: [],
  overviewCalls: 0,
  emitChrome: () => {},
  emitRecent: () => {},
};
window.desktopInspectorTest = control;

function ok(value) {
  return {ok: true, value};
}

function inspection(host, instance, includeLog) {
  const facts = HOSTS[host];
  const controlSocketPath = `/runtime/runs/${instance}/control.sock`;
  const serverLogPath = `/runtime/runs/${instance}/server.log`;
  return {
    id: instance,
    state: 'ended',
    record: null,
    metadata: {
      version: 1,
      id: instance,
      project_root: facts.project,
      run_id: facts.run,
      task: facts.task,
      pid: facts.pid,
      started_at: 1_700_000_000,
      hostname: facts.hostname,
      protocol_version: 1,
      vibesys_version: '0.1.0',
      vibesys_root: '/src/vibesys',
      control_socket_path: controlSocketPath,
      server_log_path: serverLogPath,
    },
    control_socket_path: controlSocketPath,
    server_log_path: serverLogPath,
    log: includeLog
      ? {
          state: 'available',
          text: `${facts.hostname}: retained diagnostics`,
          truncated: true,
        }
      : null,
  };
}

function listing(host, facts) {
  return {
    kind: 'listed',
    host,
    live: [],
    ended: [
      {
        id: facts.instance,
        projectRoot: facts.project,
        runId: facts.run,
        status: 'stopped',
        startedAt: 1_700_000_000,
        reason: null,
      },
    ],
    unreadable: [],
  };
}

Object.defineProperty(window, 'vibesysWelcome', {
  value: {
    platform: 'linux',
    overview: async () => {
      control.overviewCalls += 1;
      return ok({settingsProblem: null, recent: RECENT, chrome: CHROME, host: 'local'});
    },
    hosts: async () => ok(['gpu']),
    host: async host => {
      const facts = HOSTS[host];
      return ok({
        key: host,
        label: facts.label,
        checkout: '/src/vibesys',
      });
    },
    setCheckout: async () => ok('/src/vibesys'),
    signIn: async () => ok(null),
    tasks: async () => ok(['queue']),
    complete: async (_host, directory) =>
      ok({directory, entries: [], truncated: false}),
    attach: async () => ok(null),
    start: async () => ok(null),
    cancelStart: async () => ok(null),
    resume: async () => ok(null),
    showRun: async () => ok(null),
    showWelcome: async () => ok(null),
    retry: async () => ok(null),
    stop: async () => ok(null),
    inspect: async (host, instance, includeLog) => {
      control.inspectionCalls.push({host, instance, includeLog});
      return ok(inspection(host, instance, includeLog));
    },
    onChrome: listener => {
      control.emitChrome = listener;
    },
    onRecent: listener => {
      control.emitRecent = listener;
    },
    onShowHost: () => {},
    onListing: listener => {
      for (const [host, facts] of Object.entries(HOSTS)) listener(listing(host, facts));
    },
  },
});
