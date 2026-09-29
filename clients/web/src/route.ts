/**
 * Page addresses. The home page is `/?token=<home>` and its hash picks the view; a run page adds
 * the project and the gateway's WebSocket URL, which carries the gateway's own token. Moving
 * between them is a page load, so each page keeps one session (plan 3).
 */
export type HomeView =
  | {kind: 'empty'}
  | {kind: 'new'}
  | {kind: 'open' | 'resume'; projectId: string; runId: string};

export interface PageParams {
  token: string | null;
  gateway: string | null;
  project: string | null;
}

export function pageParams(href: string): PageParams {
  const search = new URL(href).searchParams;
  return {
    token: search.get('token'),
    gateway: search.get('gateway'),
    project: search.get('project'),
  };
}

const EMPTY: HomeView = {kind: 'empty'};

function runView(kind: 'open' | 'resume', value: string): HomeView {
  const slash = value.indexOf('/');
  if (slash <= 0) return EMPTY;
  try {
    return {
      kind,
      projectId: decodeURIComponent(value.slice(0, slash)),
      runId: decodeURIComponent(value.slice(slash + 1)),
    };
  } catch {
    return EMPTY;
  }
}

export function homeView(hash: string): HomeView {
  const raw = hash.replace(/^#/, '');
  const equals = raw.indexOf('=');
  const key = equals < 0 ? raw : raw.slice(0, equals);
  if (key === 'new') return {kind: 'new'};
  if (key === 'open' || key === 'resume') return runView(key, raw.slice(equals + 1));
  return EMPTY;
}

export function homeHref(token: string, view: HomeView): string {
  const base = `/?${new URLSearchParams({token})}`;
  switch (view.kind) {
    case 'empty':
      return base;
    case 'new':
      return `${base}#new`;
    case 'open':
    case 'resume':
      return `${base}#${view.kind}=${encodeURIComponent(view.projectId)}/${encodeURIComponent(view.runId)}`;
  }
}

export function runHref(token: string, projectId: string, websocketUrl: string): string {
  return `/?${new URLSearchParams({token, project: projectId, gateway: websocketUrl})}`;
}

/** What a run page opened from the home links back to. */
export interface RunLinks {
  newRun: string;
  resume: (runId: string) => string;
}

export function runLinks(token: string, projectId: string): RunLinks {
  return {
    newRun: homeHref(token, {kind: 'new'}),
    resume: runId => homeHref(token, {kind: 'resume', projectId, runId}),
  };
}
