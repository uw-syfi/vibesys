import {AgentRole, AgentStatus, JobOutcome, TurnStatus} from '@vibesys/orchestrator-proto';
import {Card, Gantt, type GanttRow, Row, Sparkline, Stack, Stat, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {fanoutOverTime, type OrchestratorState, runAgents, timeline} from '../../state/index.js';
import {agentNames, nameOf} from './names.js';
import {agentWord, formatClock, formatDuration, harnessWord, jobWord, roleWord} from './words.js';

const TICKS = 5;

function ticksOf(from: number, to: number) {
  return Array.from({length: TICKS + 1}, (_, index) => {
    const at = from + ((to - from) * index) / TICKS;
    return {at, label: formatClock(at)};
  });
}

/** Each role has its own color so the timeline can be read without the legend. */
const SERIES: Readonly<Record<AgentRole, number>> = {
  [AgentRole.UNSPECIFIED]: 1,
  [AgentRole.LEAD]: 1,
  [AgentRole.PLANNER]: 2,
  [AgentRole.WORKER]: 3,
  [AgentRole.JUDGE]: 4,
  [AgentRole.COMBINER]: 5,
  [AgentRole.PROFILER]: 6,
};

/** When each agent ran, how many ran at once, and the jobs underneath. */
export function TimelineView(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
  readonly nowMs: number;
  readonly agentId: string | null;
  readonly pick: (agentId: string) => void;
}): JSX.Element {
  const {state, runId, nowMs} = props;
  const chart = timeline(state, runId, nowMs);
  const names = agentNames(state, runId);
  const from = chart.startMs;
  const to = Math.max(chart.endMs, from + 1000);
  const rows: GanttRow[] = chart.rows.map(row => {
    const failed = row.agent.status === AgentStatus.FAILED || row.agent.status === AgentStatus.LOST;
    const running = row.bars.some(bar => bar.running);
    return {
      id: row.agent.id,
      label: `${nameOf(names, row.agent)} (${harnessWord(row.agent.harness)})`,
      start: row.bars[0]?.startMs ?? from,
      end: row.bars.reduce((end, bar) => Math.max(end, bar.endMs), from),
      outcome: failed ? 'err' : running ? 'live' : 'ok',
      figure: agentWord(row.agent.status).label,
      selected: row.agent.id === props.agentId,
      press: () => props.pick(row.agent.id),
      segments: row.bars.map(bar => ({
        id: bar.turnId,
        start: bar.startMs,
        end: bar.endMs,
        kind: bar.running ? 'hatched' : bar.status === TurnStatus.FAILED ? 'grey' : 'solid',
        series: SERIES[row.agent.role],
        label: `${roleWord(row.agent.role)}: ${formatDuration(bar.endMs - bar.startMs)}`,
      })),
    };
  });
  const jobs: GanttRow[] = chart.jobs.map(job => ({
    id: job.jobId,
    label: `Job ${job.jobId} (${nameOf(names, job.submittedBy)})`,
    start: job.startMs,
    end: job.endMs,
    outcome:
      job.outcome === JobOutcome.SUCCEEDED
        ? 'ok'
        : job.outcome === JobOutcome.FAILED
          ? 'err'
          : 'muted',
    figure: jobWord(job.outcome).label,
    press: () => props.pick(job.submittedBy),
    segments: [
      {
        id: job.jobId,
        start: job.startMs,
        end: job.endMs,
        kind: 'solid',
        label: job.detail === '' ? jobWord(job.outcome).label : job.detail,
      },
    ],
  }));
  const fanout = fanoutOverTime(state, runId);
  const peak = fanout.reduce((most, point) => Math.max(most, point.live), 0);
  const low = fanout.reduce((least, point) => Math.min(least, point.live), peak);
  const agentCount = runAgents(state, runId).length;
  return (
    <Stack gap="space-6">
      <Row gap="space-6" wrap>
        <Stat inline label="Agents so far" value={String(agentCount)} />
        <Stat inline label="Most at once" value={String(peak)} />
        <Stat inline label="Run time" value={formatDuration(to - from)} />
      </Row>
      <Card section="When each agent worked">
        <Gantt
          label="When each agent worked"
          from={from}
          to={to}
          {...(nowMs > from && nowMs <= to ? {playhead: nowMs} : {})}
          ticks={ticksOf(from, to)}
          rows={rows}
          nameLabel="Agent"
          figureLabel="Now"
        />
      </Card>
      <Card section="How many agents ran at once">
        {fanout.length === 0 ? (
          <Text tone="muted">No helpers have started yet.</Text>
        ) : (
          <Stack gap="space-2">
            <Sparkline
              label="Agents running at once over the run"
              steps={fanout.map(point => ({x: point.atMs, y: point.live}))}
              from={from}
              to={to}
            />
            <Row gap="space-3" justify="between">
              <Text tone="muted" variant="small">{`${formatClock(from)} UTC`}</Text>
              <Text tone="muted" variant="small">{`${formatClock(to)} UTC`}</Text>
            </Row>
            <Text tone="muted" variant="small">
              {`Left to right is time. The higher the line, the more agents were working at the same moment: from ${low} up to ${peak}.`}
            </Text>
          </Stack>
        )}
      </Card>
      <Card section="Jobs">
        {jobs.length === 0 ? (
          <Text tone="muted">No jobs have finished yet.</Text>
        ) : (
          <Gantt
            label="Jobs the agents started"
            from={from}
            to={to}
            ticks={ticksOf(from, to)}
            rows={jobs}
            nameLabel="Job"
            figureLabel="Result"
          />
        )}
      </Card>
    </Stack>
  );
}
