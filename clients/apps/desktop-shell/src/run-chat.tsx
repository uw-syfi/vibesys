/**
 * The Run screen with its chat: composition of the run page and the run chat, which are peers.
 * The chat reaches the run through the page's attached session (`page.chat`) and the host's
 * harness catalogue through the welcome bridge; it reads the run from the page's fold.
 */
import {ChatPanel, type RunChatPorts, useRunChat} from '@vibesys/feature-run-chat/renderer';
import {RunScreen, type useRunPage} from '@vibesys/feature-run-page/renderer';
import {ideaOfAttempt, runHasEnded} from '@vibesys/feature-run-page/state';
import {settled, type WelcomeBridge} from '@vibesys/ipc';
import type {RunRef} from '@vibesys/runs';
import {type JSX, useEffect, useMemo, useState} from 'react';

type RunPageView = NonNullable<ReturnType<typeof useRunPage>>;

function chatPorts(page: RunPageView['chat'], bridge: WelcomeBridge, run: RunRef): RunChatPorts {
  return {
    harnesses: async () => {
      const result = await settled(bridge.harnesses(run.host));
      return result.ok ? result.value : {kind: 'unsupported'};
    },
    createThread: (harness, signal) => page.createThread(harness, signal),
    ask: (input, signal) => page.ask(input, signal),
    control: request => page.control(request),
    newControlId: () => crypto.randomUUID(),
  };
}

/** The word on a Chat button that cannot open: the machine's VibeSys cannot serve an ended run. */
const NEEDS_UPDATE = 'Needs an update';

/** What the run's machine offers for chat on an ended run; null until it said. */
interface EndedChat {
  /** The run can be read: its reader serves its threads. */
  readonly reads: boolean;
  /** The reader also answers questions and opens chats over the saved run. */
  readonly asks: boolean;
}

/** What the run's machine offers for chat on an ended run, asked once the run has ended. */
function useChatsEnded(bridge: WelcomeBridge, run: RunRef, ended: boolean): EndedChat | null {
  const [offered, setOffered] = useState<EndedChat | null>(null);
  useEffect(() => {
    if (!ended) return;
    let current = true;
    void settled(bridge.helperFeatures(run.host)).then(result => {
      if (!current) return;
      setOffered(
        result.ok
          ? {
              reads: result.value.includes('chat_ended_runs'),
              asks: result.value.includes('chat_ended_run_questions'),
            }
          : null,
      );
    });
    return () => {
      current = false;
    };
  }, [bridge, run.host, ended]);
  return offered;
}

/** The Run screen over `page`, with the Chat button and its panel. */
export function RunWithChat(props: {
  readonly page: RunPageView;
  readonly run: RunRef;
  readonly bridge: WelcomeBridge;
}): JSX.Element {
  const {page, run, bridge} = props;
  const ports = useMemo(() => chatPorts(page.chat, bridge, run), [page.chat, bridge, run]);
  const ideaOf = useMemo(() => ideaOfAttempt(page.whole.candidates.candidates), [page.whole]);
  const roles = useMemo(() => page.whole.agents.agents.map(agent => agent.role), [page.whole]);
  const ended = runHasEnded(page.state);
  const chatsEnded = useChatsEnded(bridge, run, ended);
  const chat = useRunChat({
    identity: `${run.host} ${run.runId}`,
    core: ended && page.readerCore !== null ? page.readerCore : page.core,
    ports,
    ideaOf,
    roles,
    readOnly: ended,
    askable: ended && chatsEnded?.asks === true && page.state.reader.link.kind === 'attached',
  });
  const {dispatch} = page;
  useEffect(() => {
    dispatch({type: 'chat', open: chat.open});
  }, [dispatch, chat.open]);
  const unsupported =
    ended && (chatsEnded?.reads === false || page.state.reader.link.kind === 'unsupported');
  return (
    <RunScreen
      state={page.state}
      model={page.model}
      whole={page.whole}
      core={page.core}
      route={page.route}
      zoom={page.zoom}
      dispatch={page.dispatch}
      modelNames={page.modelNames}
      chat={{
        open: chat.open,
        toggle: chat.toggle,
        panel: chat.panel === null ? null : <ChatPanel {...chat.panel} />,
        ...(unsupported ? {unavailable: NEEDS_UPDATE} : {}),
      }}
    />
  );
}
