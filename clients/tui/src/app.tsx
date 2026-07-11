import React, {useCallback, useEffect, useRef, useState} from 'react';
import {Box, Text, useApp, useInput, useStdout} from 'ink';
import TextInput from 'ink-text-input';
import {HELP_TEXT, parseInput} from './commands.js';
import {SupervisionClient} from './client.js';
import type {ProtocolResponse, RequestInput, RunEvent, RunSnapshot} from './protocol.js';

type Props = {client: SupervisionClient};

export function App({client}: Props) {
  const {exit} = useApp();
  const {stdout} = useStdout();
  const sequence = useRef(0);
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [liveContent, setLiveContent] = useState('Waiting for run output…');
  const [detailContent, setDetailContent] = useState('');
  const [view, setView] = useState('live');
  const [input, setInput] = useState('');

  const refresh = useCallback(async () => {
    const [snapshotResponse, eventResponse] = await Promise.all([
      client.request({type: 'query.snapshot'}),
      client.request({type: 'query.events', after_sequence: sequence.current, timeout_ms: 0}),
    ]);
    if (snapshotResponse.snapshot) {
      setSnapshot(previous => snapshotsEqual(previous, snapshotResponse.snapshot!)
        ? previous : snapshotResponse.snapshot!);
    }
    const events = eventResponse.events ?? [];
    if (events.length) {
      sequence.current = Math.max(sequence.current, ...events.map(event => event.sequence ?? 0));
      const output = events.flatMap(event =>
        event.type === 'output' && event.data?.kind === 'output' ? [event.data.content] : [],
      ).join('');
      if (output) setLiveContent(previous => appendLiveOutput(previous, output));
    }
  }, [client]);

  useEffect(() => {
    const timer = setInterval(() => void refresh().catch(showError), 250);
    void refresh().catch(showError);
    return () => clearInterval(timer);
  }, [refresh]);

  useInput((character, key) => {
    if (key.ctrl && character === 'l') setView('live');
    if (key.ctrl && character === 'c') exit();
  });

  function showError(error: unknown) {
    setDetailContent(String(error));
    setView('error');
  }

  async function submit(value: string) {
    setInput('');
    const parsed = parseInput(value.trim());
    if (parsed.error) return showError(parsed.error);
    if (parsed.localView === 'live') return setView('live');
    if (parsed.localView === 'help') {
      setDetailContent(HELP_TEXT);
      return setView('help');
    }
    if (!parsed.request) return;
    try {
      const response = await client.request(parsed.request);
      const rendered = renderResponse(parsed.request, response);
      if (rendered !== null) {
        setDetailContent(rendered);
        setView(parsed.request.type ?? 'detail');
      }
    } catch (error) {
      showError(error);
    }
  }

  const status = snapshot
    ? `${snapshot.status} · ${snapshot.agent_kind ?? 'starting'} · ${snapshot.round_label ?? 'no round yet'} · ${snapshot.pending_steering} steering pending`
    : 'connecting';
  const terminalRows = stdout.rows ?? 24;
  const outputRows = Math.max(3, terminalRows - 5);
  const content = view === 'live' ? liveContent : detailContent;
  const visible = content.split('\n').slice(-outputRows).join('\n');

  return <Box flexDirection="column" height={terminalRows} overflowY="hidden">
    <Box flexShrink={0}><Text bold color="cyan">VibeServe · {status}</Text></Box>
    <Box borderStyle="round" height={outputRows + 2} overflowY="hidden" paddingX={1}>
      <Text>{visible}</Text>
    </Box>
    <Box flexShrink={0} height={1} overflowY="hidden">
      <Text dimColor wrap="truncate-end">Ask a question or use /steer, /pause, /resume, /live, /history, /help</Text>
    </Box>
    <Box flexShrink={0} height={1} overflowY="hidden">
      <Text color="green">› </Text>
      <TextInput value={input} onChange={setInput} onSubmit={submit}/>
    </Box>
  </Box>;
}

function renderResponse(request: RequestInput, response: ProtocolResponse): string | null {
  if (response.ack) {
    const id = response.ack.resource_id ? ` ${response.ack.resource_id.slice(0, 8)}` : '';
    return `${response.ack.action}${id}: ${response.ack.status}`;
  }
  if (response.chat) return `you: ${response.chat.question}\nvibeserve: ${response.chat.answer}`;
  if (response.round) {
    if (!response.round.blocks?.length) return `No data for round ${response.round.round_number}.`;
    return response.round.blocks.map(block => `${block.source}\n${block.content}`).join('\n\n');
  }
  if (response.artifact) return `${response.artifact.path}\n${response.artifact.content}`;
  if (response.events?.length) return response.events.map(renderEvent).join('\n');
  if (request.type === 'query.history' || request.type === 'query.invocation') return 'No events found.';
  return null;
}

function renderEvent(event: RunEvent): string {
  const target = [event.round_label, event.agent_kind].filter(Boolean).join(' / ');
  const invocation = event.invocation_id ? ` [${event.invocation_id.slice(0, 8)}]` : '';
  return `${event.timestamp} ${event.type}${invocation} ${target} ${event.text ?? ''}`.trim();
}

function snapshotsEqual(left: RunSnapshot | null, right: RunSnapshot): boolean {
  return left?.sequence === right.sequence
    && left.status === right.status
    && left.agent_kind === right.agent_kind
    && left.round_label === right.round_label
    && left.pending_steering === right.pending_steering
    && left.last_consumed_steering === right.last_consumed_steering;
}

function appendLiveOutput(previous: string, next: string): string {
  const current = previous === 'Waiting for run output…' ? '' : previous;
  return `${current}${next}`.slice(-200_000);
}
