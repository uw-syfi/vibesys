import {
  BoxRenderable,
  InputRenderable,
  ScrollBoxRenderable,
  TextRenderable,
  type CliRenderer,
  type KeyEvent,
} from '@opentui/core';
import {HELP_TEXT, parseInput} from './commands.js';
import type {ProtocolResponse, RequestInput, RunEvent, RunSnapshot} from './protocol.js';

export interface SupervisionClientLike {
  request(input: RequestInput): Promise<ProtocolResponse>;
}

export interface OpenTuiApp {
  destroy(): void;
}

export function createOpenTuiApp(
  renderer: CliRenderer,
  client: SupervisionClientLike,
): OpenTuiApp {
  let sequence = 0;
  let snapshot: RunSnapshot | null = null;
  let liveContent = 'Waiting for run output…';
  let detailContent = '';
  let view = 'live';
  let exitScheduled = false;

  const root = new BoxRenderable(renderer, {
    id: 'app',
    width: '100%',
    height: '100%',
    flexDirection: 'column',
  });
  const header = new TextRenderable(renderer, {
    id: 'header',
    height: 1,
    fg: '#22d3ee',
    content: 'VibeServe · connecting',
  });
  const output = new TextRenderable(renderer, {
    id: 'output',
    width: '100%',
    content: liveContent,
  });
  const viewport = new ScrollBoxRenderable(renderer, {
    id: 'viewport',
    width: '100%',
    flexGrow: 1,
    border: true,
    borderStyle: 'rounded',
    borderColor: '#475569',
    stickyScroll: true,
    stickyStart: 'bottom',
    viewportCulling: true,
    verticalScrollbarOptions: {showArrows: true},
  });
  const help = new TextRenderable(renderer, {
    id: 'key-help',
    height: 1,
    fg: '#64748b',
    content: '↑/↓ · PgUp/PgDn · Home/End · Ctrl+L: live · /help',
  });
  const inputBox = new BoxRenderable(renderer, {
    id: 'input-box',
    height: 3,
    width: '100%',
    border: true,
    borderStyle: 'rounded',
    borderColor: '#22c55e',
    title: ' Ask or command ',
    paddingLeft: 1,
    paddingRight: 1,
  });
  const input = new InputRenderable(renderer, {
    id: 'input',
    width: '100%',
    placeholder: 'Type a question or /help',
    textColor: '#f8fafc',
    focusedTextColor: '#f8fafc',
    onSubmit: () => void submit(input.value),
  });

  viewport.add(output);
  inputBox.add(input);
  root.add(header);
  root.add(viewport);
  root.add(help);
  root.add(inputBox);
  renderer.root.add(root);
  input.focus();

  function renderContent(): void {
    output.content = view === 'live' ? liveContent : detailContent;
  }

  function showView(nextView: string, content?: string): void {
    view = nextView;
    if (content !== undefined) detailContent = content;
    renderContent();
    viewport.scrollTo(viewport.scrollHeight);
  }

  function showError(error: unknown): void {
    showView('error', String(error));
  }

  async function refresh(): Promise<void> {
    const [snapshotResponse, eventResponse] = await Promise.all([
      client.request({type: 'query.snapshot'}),
      client.request({type: 'query.events', after_sequence: sequence, timeout_ms: 0}),
    ]);
    if (snapshotResponse.snapshot) snapshot = snapshotResponse.snapshot;
    const events = eventResponse.events ?? [];
    if (events.length > 0) {
      sequence = Math.max(sequence, ...events.map(event => event.sequence ?? 0));
      const streamed = events.flatMap(event =>
        event.type === 'output' && event.data?.kind === 'output' ? [event.data.content] : []
      ).join('');
      if (streamed) {
        liveContent = appendLiveOutput(liveContent, streamed);
        if (view === 'live') renderContent();
      }
    }
    const status = snapshot
      ? `${snapshot.status} · ${snapshot.agent_kind ?? 'starting'} · ${snapshot.round_label ?? 'no round yet'}`
      : 'connecting';
    header.content = `VibeServe · ${status}`;
    if (!exitScheduled && (snapshot?.status === 'completed' || snapshot?.status === 'failed')) {
      exitScheduled = true;
      setTimeout(() => renderer.destroy(), 100);
    }
  }

  async function submit(value: string): Promise<void> {
    input.value = '';
    const parsed = parseInput(value.trim());
    if (parsed.error) return showError(parsed.error);
    if (parsed.localView === 'live') return showView('live');
    if (parsed.localView === 'help') return showView('help', HELP_TEXT);
    if (!parsed.request) return;
    try {
      const response = await client.request(parsed.request);
      const rendered = renderResponse(parsed.request, response);
      if (rendered !== null) showView(parsed.request.type ?? 'detail', rendered);
    } catch (error) {
      showError(error);
    }
  }

  function onKey(key: KeyEvent): void {
    if (key.ctrl && key.name === 'l') {
      showView('live');
      key.preventDefault();
      return;
    }
    if (key.name === 'pageup') viewport.scrollBy(-1, 'viewport');
    else if (key.name === 'pagedown') viewport.scrollBy(1, 'viewport');
    else if (key.ctrl && key.name === 'up') viewport.scrollBy(-1);
    else if (key.ctrl && key.name === 'down') viewport.scrollBy(1);
    else if (key.name === 'home') viewport.scrollTo(0);
    else if (key.name === 'end') viewport.scrollTo(viewport.scrollHeight);
    else return;
    key.preventDefault();
  }

  renderer.keyInput.on('keypress', onKey);
  const timer = setInterval(() => void refresh().catch(showError), 250);
  void refresh().catch(showError);

  return {
    destroy(): void {
      clearInterval(timer);
      renderer.keyInput.off('keypress', onKey);
      root.destroyRecursively();
    },
  };
}

function renderResponse(request: RequestInput, response: ProtocolResponse): string | null {
  if (response.ack) return `${response.ack.action}: ${response.ack.status}`;
  if (response.chat) return `you: ${response.chat.question}\nvibeserve: ${response.chat.answer}`;
  if (response.events?.length) return response.events.map(renderEvent).join('\n');
  if (request.type === 'query.history') return 'No events found.';
  return null;
}

function renderEvent(event: RunEvent): string {
  const target = [event.round_label, event.agent_kind].filter(Boolean).join(' / ');
  const invocation = event.invocation_id ? ` [${event.invocation_id.slice(0, 8)}]` : '';
  return `${event.timestamp} ${event.type}${invocation} ${target} ${event.text ?? ''}`.trim();
}

function appendLiveOutput(previous: string, next: string): string {
  const current = previous === 'Waiting for run output…' ? '' : previous;
  return `${current}${next}`.slice(-200_000);
}
