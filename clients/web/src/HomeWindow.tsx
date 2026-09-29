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
import {Resizer} from './ui/Resizer.js';
import {NewRunRow, SearchRow, Sidebar} from './ui/Sidebar.js';
import {SidebarToggle, Titlebar, TitleLead} from './ui/TitleRow.js';
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
      <Titlebar />
      <p className="empty">Select a run, or choose New run in the sidebar</p>
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
        loop: run?.loop ?? null,
        recorded: run?.budget ?? null,
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
  // As in the run window: the sidebar hides and resizes. Home has no pane, so it always fits.
  const [sidebar, setSidebar] = useState(true);
  const [sideWidth, setSideWidth] = useState<number>(SIDE.initial);
  const shownRun = view.kind === 'open' || view.kind === 'resume' ? view.runId : null;
  const onRun = ({intent}: PaletteItem) => {
    setPalette(false);
    if (intent.kind === 'newRun') window.location.assign(newRun);
    if (intent.kind === 'open') window.location.assign(intent.href);
  };
  return (
    <div className="win">
      {sidebar ? (
        <Sidebar
          width={sideWidth}
          sections={sections}
          current={null}
          selectedRun={shownRun}
          summary={NO_ROUNDS}
          selected={null}
          now={new Date()}
          onRound={() => undefined}
          head={<SidebarToggle shown onToggle={() => setSidebar(false)} />}
          nav={
            <>
              <NewRunRow href={newRun} on={view.kind === 'new'} />
              <SearchRow onOpen={openPalette} />
            </>
          }
          resizer={
            <Resizer
              label="Resize the sidebar"
              edge="right"
              value={sideWidth}
              min={SIDE.min}
              max={SIDE.max}
              grow={1}
              widthAt={clientX => clientX}
              onChange={width => setSideWidth(Math.min(SIDE.max, Math.max(SIDE.min, width)))}
            />
          }
        />
      ) : null}
      <main className="main">
        <TitleLead.Provider
          value={sidebar ? null : <SidebarToggle shown={false} onToggle={() => setSidebar(true)} />}
        >
          <HomeMain view={view} client={client} token={token} listing={listing} />
        </TitleLead.Provider>
      </main>
      {palette ? (
        <Palette
          items={homePaletteItems(sections, view.kind !== 'new')}
          placeholder="Search commands and runs…"
          onRun={onRun}
          onClose={() => setPalette(false)}
        />
      ) : null}
    </div>
  );
}
