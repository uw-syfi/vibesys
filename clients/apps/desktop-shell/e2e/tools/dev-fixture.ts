/**
 * The desktop shell's renderer in a browser, with hot reload, over a run fixture: Vite's dev server
 * serves `shell.html`, and every page it serves first runs the same Fake bridge and fixture server
 * the e2e journeys install (`installFixture`), then opens the fixture's run from Recent and plays
 * a live fixture's later events. Edit a feature's source and the page updates in place.
 *
 *   pnpm --filter @vibesys/app-desktop-shell dev:fixture [NAME] [--port N] [--theme ink|paper]
 *   pnpm --filter @vibesys/app-desktop-shell dev:fixture --list
 *
 * NAME is a fixture of `@vibesys/run-fixtures` (default `weeks-600-ideas`). Visiting
 * `/fixture/NAME[?theme=paper]` picks another fixture or theme without a restart (the choice rides
 * in a cookie, since the shell rejects query parameters it does not know).
 */
import {fileURLToPath} from 'node:url';
import {parseArgs} from 'node:util';
import {
  FIXTURE_NAMES,
  fixtureNamed,
  objectivesRun,
  type RunFixture,
  splitLive,
} from '@vibesys/run-fixtures';
import {createServer, type Plugin} from 'vite';
import {fixtureScripts} from '../fixture-world.js';

const DEFAULT_FIXTURE = 'weeks-600-ideas';
const SCRIPT_PATH = '/__fixture.js';
const SHELL_ROOT = fileURLToPath(new URL('../../', import.meta.url));

type Theme = 'ink' | 'paper';

/** The catalog's fixtures, and the generated runs of three and six objectives (`objectives-3`, `objectives-6`). */
function runNamed(name: string): RunFixture {
  if (name === 'objectives-3') return objectivesRun(3);
  if (name === 'objectives-6') return objectivesRun(6);
  return fixtureNamed(name);
}

function themeOf(text: string | null): Theme {
  return text === 'paper' ? 'paper' : 'ink';
}

/** The classic script a page runs first: the installers, then opening the run from Recent. */
function pageScript(name: string, theme: Theme): string {
  const fixture = runNamed(name);
  const parts = [...fixtureScripts(fixture, {theme})];
  const live = fixture.state === 'live' ? splitLive(fixture) : null;
  const play = JSON.stringify(live === null ? [] : [live.beforeDrop, live.afterDrop]);
  const drops = fixture.dropAfter !== undefined;
  parts.push(`(${openRun.toString()})(${play}, ${drops});`);
  return parts.join('\n');
}

/** In the page: opens the run Recent lists, then streams a live fixture's later batches. */
function openRun(batches: readonly (readonly unknown[])[], drops: boolean): void {
  type Host = {emit(events: readonly unknown[]): void; drop(): void};
  const link = 'main button.vs-overview-tile-press';
  const watch = new MutationObserver(() => {
    const button = document.querySelector<HTMLButtonElement>(link);
    if (button === null) return;
    watch.disconnect();
    button.click();
    const host = (window as unknown as {fakeHost: Host}).fakeHost;
    const [before, after] = batches;
    if (before !== undefined) host.emit(before);
    if (drops) host.drop();
    if (after !== undefined) host.emit(after);
  });
  watch.observe(document, {childList: true, subtree: true});
}

/** What the page's cookie says: the shell rejects query parameters it does not know, so the choice rides here. */
function choiceOf(cookie: string | undefined, initial: string): {name: string; theme: Theme} {
  const pairs = new Map(
    (cookie ?? '').split(/;\s*/).map(pair => pair.split('=') as [string, string | undefined]),
  );
  return {name: pairs.get('fixture') ?? initial, theme: themeOf(pairs.get('theme') ?? null)};
}

function fixtureApp(initial: string, theme: Theme): Plugin {
  return {
    name: 'vibesys-fixture-app',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const url = new URL(request.url ?? '/', 'http://localhost');
        const chosen = /^\/fixture\/([^/]+)$/.exec(url.pathname);
        if (url.pathname === '/' || chosen !== null) {
          const cookies = [
            `fixture=${chosen?.[1] ?? initial}; Path=/`,
            `theme=${url.searchParams.get('theme') ?? theme}; Path=/`,
          ];
          response.statusCode = 302;
          response.setHeader('set-cookie', cookies);
          response.setHeader('location', '/shell.html');
          response.end();
          return;
        }
        if (url.pathname !== SCRIPT_PATH) {
          next();
          return;
        }
        try {
          const {name, theme: chosenTheme} = choiceOf(request.headers.cookie, initial);
          response.setHeader('content-type', 'text/javascript; charset=utf-8');
          response.end(pageScript(name, chosenTheme));
        } catch (error) {
          response.statusCode = 404;
          response.end(String(error));
        }
      });
    },
    transformIndexHtml: {
      order: 'pre',
      handler: () => [{tag: 'script', attrs: {src: SCRIPT_PATH}, injectTo: 'head-prepend'}],
    },
  };
}

const {values, positionals} = parseArgs({
  allowPositionals: true,
  options: {
    list: {type: 'boolean', default: false},
    port: {type: 'string', default: '5173'},
    theme: {type: 'string', default: 'ink'},
  },
});

if (values.list) {
  process.stdout.write(`${[...FIXTURE_NAMES, 'objectives-3', 'objectives-6'].join('\n')}\n`);
} else {
  const name = positionals[0] ?? DEFAULT_FIXTURE;
  runNamed(name);
  const server = await createServer({
    root: SHELL_ROOT,
    plugins: [fixtureApp(name, themeOf(values.theme))],
    server: {host: '127.0.0.1', port: Number(values.port)},
  });
  await server.listen();
  const address = server.resolvedUrls?.local[0] ?? '';
  process.stdout.write(
    `fixture ${name}: ${address}\n` +
      `another fixture: ${address}fixture/NAME[?theme=paper] (see --list)\n`,
  );
}
