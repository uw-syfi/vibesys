/** The home page: every project's runs in the sidebar, and the view the URL hash names. */
import {initialCoreState} from '@vibesys/core-state';
import {useEffect, useMemo, useState} from 'react';
import {httpHomeApi, sidebarSections} from './home.js';
import type {HomeClient} from './home-api.js';
import {useHome, useNewRunShortcut} from './home-hooks.js';
import {runSummary} from './rounds.js';
import {type HomeView, homeHref, homeView} from './route.js';
import {NewRunRow, Sidebar} from './ui/Sidebar.js';
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
  return (
    <div className="win">
      <Sidebar
        width={SIDE.initial}
        sections={sidebarSections(listing.projects, listing.runs, null)}
        current={null}
        summary={NO_ROUNDS}
        selected={null}
        now={new Date()}
        onRound={() => undefined}
        nav={<NewRunRow href={newRun} on={view.kind === 'new'} />}
      />
      <main className="main">
        <EmptyHome />
      </main>
    </div>
  );
}
