const LOCAL_INSTANCE = '0123456789ab';
const SSH_INSTANCE = 'ba9876543210';
let chrome = {mode: 'welcome', attached: null, stops: {}};
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
let recent = Object.entries(HOSTS).map(([host, facts], index) => ({
  host,
  hostLabel: facts.label,
  project: facts.project,
  task: facts.task,
  instanceId: facts.instance,
  runId: facts.run,
  attachedAt: 2 - index,
}));
const listings = new Map(
  Object.entries(HOSTS).map(([host, facts]) => [host, listing(host, facts)]),
);
let firstHost = 'local';
let sequence = 0;

const control = {
  inspectionCalls: [],
  overviewCalls: 0,
  emitChrome: () => {},
  emitRecent: () => {},
  emitShowHost: () => {},
  emitListing: () => {},
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
    version: 1,
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
    unverified: [],
  };
}

function event(value) {
  sequence += 1;
  return {sequence, value};
}

Object.defineProperty(window, 'vibesysWelcome', {
  value: {
    platform: 'linux',
    overview: async () => {
      control.overviewCalls += 1;
      return ok({
        eventCursor: sequence,
        settingsProblem: null,
        recent,
        listings: [...listings.values()],
        chrome,
        host: firstHost,
      });
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
    cancelCheckout: async () => ok(null),
    cancelInspection: async () => ok(null),
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
    inspect: async (host, instance, includeLog, inspectionToken) => {
      control.inspectionCalls.push({host, instance, includeLog, inspectionToken});
      return ok(inspection(host, instance, includeLog));
    },
    onChrome: listener => {
      control.emitChrome = value => {
        chrome = value;
        listener(event(value));
      };
    },
    onRecent: listener => {
      control.emitRecent = value => {
        recent = value;
        listener(event(value));
      };
    },
    onShowHost: listener => {
      control.emitShowHost = value => {
        firstHost = value;
        listener(event(value));
      };
    },
    onListing: listener => {
      control.emitListing = value => {
        listings.set(value.host, value);
        listener(event(value));
      };
      for (const value of listings.values()) listener(event(value));
    },
  },
});
