/** The home page: every project's runs in the sidebar, and the view the URL hash names. */
import {initialCoreState} from '@vibesys/core-state';
import {useCallback, useEffect, useMemo, useState} from 'react';
import {httpHomeApi, type Listing, sidebarSections} from './home.js';
import type {HomeClient} from './home-api.js';
import {useHome, useNewRunShortcut, usePaletteShortcut} from './home-hooks.js';
import {homePaletteItems, type PaletteItem} from './palette.js';
import {ReopenView, ResumeView} from './RunEntry.js';
import {runSummary} from './rounds.js';
import {type HomeView, homeHref, homeView} from './route.js';
import {SetupView} from './SetupView.js';
import {Palette} from './ui/Palette.js';
import {NewRunRow, SearchRow, Sidebar} from './ui/Sidebar.js';
import {SIDE} from './ui-state.js';
import './window.css';

/** No run is open here, so the sidebar lists no rounds. */
const NO_ROUNDS = runSummary(initialCoreState(), [], [], null);

function useHashView(): HomeView {
  const [view, setView] = useState(() => homeView(window.location.hash));
  useEffect(() => {
    const onHash = () => setView(homeView(window.location.hash));
    addEventListener('hashchange', onHash);
    return () => removeEventListener('hashchange', onHash);
  }, []);
  return view;
}

function EmptyHome() {
  return (
    <>
      <header className="titlebar" />
      <p className="empty">Select a run, or start one with ⌘N</p>
    </>
  );
}

interface HomeMainProps {
  view: HomeView;
  client: HomeClient;
  token: string;
  listing: Listing;
}

function HomeMain({view, client, token, listing}: HomeMainProps) {
  switch (view.kind) {
    case 'empty':
      return <EmptyHome />;
    case 'new':
      return <SetupView client={client} token={token} />;
    case 'open':
    case 'resume': {
      const {projectId, runId} = view;
      const run = listing.runs.find(item => item.id === runId && item.projectId === projectId);
      const project = listing.projects.find(item => item.id === projectId);
      const props = {
        client,
        token,
        projectId,
        runId,
        title: run?.title ?? runId,
        root: project?.path ?? null,
      };
      const key = `${view.kind}:${projectId}/${runId}`;
      return view.kind === 'open' ? (
        <ReopenView key={key} {...props} />
      ) : (
        <ResumeView key={key} {...props} />
      );
    }
  }
}

export interface HomeWindowProps {
  client: HomeClient;
  token: string;
}

export function HomeWindow({client, token}: HomeWindowProps) {
  const home = useMemo(() => httpHomeApi(client, token), [client, token]);
  const listing = useHome(home);
  const view = useHashView();
  const newRun = homeHref(token, {kind: 'new'});
  useNewRunShortcut(newRun);
  const [palette, setPalette] = useState(false);
  const openPalette = useCallback(() => setPalette(true), []);
  usePaletteShortcut(openPalette);
  const sections = sidebarSections(listing.projects, listing.runs, null);
  const onRun = ({intent}: PaletteItem) => {
    setPalette(false);
    if (intent.kind === 'newRun') window.location.assign(newRun);
    if (intent.kind === 'open') window.location.assign(intent.href);
  };
  return (
    <div className="win">
      <Sidebar
        width={SIDE.initial}
        sections={sections}
        current={null}
        summary={NO_ROUNDS}
        selected={null}
        now={new Date()}
        onRound={() => undefined}
        nav={
          <>
            <NewRunRow href={newRun} on={view.kind === 'new'} />
            <SearchRow onOpen={openPalette} />
          </>
        }
      />
      <main className="main">
        <HomeMain view={view} client={client} token={token} listing={listing} />
      </main>
      {palette ? (
        <Palette
          items={homePaletteItems(sections, view.kind !== 'new')}
          onRun={onRun}
          onClose={() => setPalette(false)}
        />
      ) : null}
    </div>
  );
}
