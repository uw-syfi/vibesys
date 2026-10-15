import {type CSSProperties, type JSX, useState} from 'react';
import type {CampaignRecord} from '../../state/campaign-record.js';
import type {CampaignViewModel} from '../../state/campaign-view.js';
import {Button} from './components/button.js';

type Workstream = CampaignRecord['workstreams'][number];
interface TimeWindow {
  readonly start: number;
  readonly end: number;
}
interface TimelineClip {
  readonly id: string;
  readonly label: string;
  readonly start: number;
  readonly end: number;
  readonly state: 'active' | 'accepted' | 'rejected' | 'activity';
  readonly selected: boolean;
}
interface TimelineTrack {
  readonly id: string;
  readonly label: string;
  readonly clips: TimelineClip[];
}

/** Internal campaign timeline: read-only intervals; seeking selects an existing measurement. */
export function CampaignTimeline({
  scenario,
  campaign,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState(0);
  const {start, end} = campaign.timeline;
  const duration = Math.max(1, end - start);
  const span = duration / zoom;
  const window = {
    start: start + Math.min(offset, duration - span),
    end: start + Math.min(offset, duration - span) + span,
  };
  const cursor = Date.parse(campaign.latestTimestamp);
  const tracks =
    campaign.timelineMode === 'workstreams'
      ? workstreamTracks(scenario.workstreams, cursor, campaign.activeMeasurement?.workstreamId)
      : agentTracks(scenario, cursor);
  const changeZoom = (next: number): void => {
    const anchor = Math.max(window.start, Math.min(window.end, cursor));
    setZoom(next);
    setOffset(
      Math.max(0, Math.min(duration - duration / next, anchor - start - duration / next / 2)),
    );
  };
  return (
    <section className="section-block timeline-section" aria-labelledby="timeline-title">
      <TimelineHeader campaign={campaign} zoom={zoom} onZoom={changeZoom} />
      <div
        className={`timeline-card ${campaign.timelineMode === 'agents' ? 'agent-timeline-card' : ''}`}
      >
        <TimelineRuler
          window={window}
          cursor={cursor}
          campaign={campaign}
          onSeek={timestamp =>
            setOffset(Math.max(0, Math.min(duration - span, timestamp - start - span / 2)))
          }
        />
        <section className="timeline-track-viewport" aria-label="Timeline tracks">
          <div className="timeline-tracks">
            {tracks.map(track => (
              <TimelineTrackRow
                key={track.id}
                track={track}
                window={window}
                cursor={cursor}
                onSelect={
                  campaign.timelineMode === 'workstreams'
                    ? campaign.setSelectedWorkstreamId
                    : campaign.setSelectedAgentId
                }
                agentMode={campaign.timelineMode === 'agents'}
              />
            ))}
          </div>
        </section>
        <div className="timeline-footer">
          <span>{timeLabel(cursor)} UTC</span>
          <div className="timeline-pan">
            <span>{zoom > 1 ? 'Pan' : 'Full run'}</span>
            {zoom > 1 && (
              <input
                type="range"
                aria-label="Pan timeline"
                min={0}
                max={duration - span}
                step={Math.max(1, duration / 1000)}
                value={window.start - start}
                onChange={event => setOffset(Number(event.target.value))}
              />
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

function TimelineHeader({
  campaign,
  zoom,
  onZoom,
}: {
  readonly campaign: CampaignViewModel;
  readonly zoom: number;
  readonly onZoom: (zoom: number) => void;
}): JSX.Element {
  return (
    <div className="section-head timeline-head">
      <h2 id="timeline-title">
        {campaign.timelineMode === 'workstreams'
          ? 'Workstream timeline'
          : 'Agent activity timeline'}
      </h2>
      <div className="timeline-tools">
        <fieldset className="segmented-control">
          <legend className="sr-only">Timeline mode</legend>
          <button
            type="button"
            className={campaign.timelineMode === 'workstreams' ? 'selected' : ''}
            aria-pressed={campaign.timelineMode === 'workstreams'}
            onClick={() => campaign.setTimelineMode('workstreams')}
          >
            Workstreams
          </button>
          <button
            type="button"
            className={campaign.timelineMode === 'agents' ? 'selected' : ''}
            aria-pressed={campaign.timelineMode === 'agents'}
            onClick={() => campaign.setTimelineMode('agents')}
          >
            Agent activity
          </button>
        </fieldset>
        <fieldset className="timeline-zoom">
          <legend className="sr-only">Timeline zoom</legend>
          <Button
            size="sm"
            variant="ghost"
            aria-label="Zoom out timeline"
            disabled={zoom === 1}
            onClick={() => onZoom(zoom / 2)}
          >
            −
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label="Fit timeline"
            aria-pressed={zoom === 1}
            onClick={() => onZoom(1)}
          >
            Fit
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label="Zoom in timeline"
            disabled={zoom === 16}
            onClick={() => onZoom(zoom * 2)}
          >
            +
          </Button>
        </fieldset>
      </div>
    </div>
  );
}

function TimelineRuler({
  window,
  cursor,
  campaign,
  onSeek,
}: {
  readonly window: TimeWindow;
  readonly cursor: number;
  readonly campaign: CampaignViewModel;
  readonly onSeek: (timestamp: number) => void;
}): JSX.Element {
  const select = (index: number): void => {
    const target = Math.max(0, Math.min(campaign.orderedMeasurements.length - 1, index));
    const timestamp = Date.parse(
      campaign.orderedMeasurements[target]?.timestamp ?? campaign.latestTimestamp,
    );
    campaign.setPlaying(false);
    campaign.setPointIndex(target);
    if (timestamp < window.start || timestamp > window.end) onSeek(timestamp);
  };
  const marks = Array.from(
    {length: 5},
    (_, index) => window.start + ((window.end - window.start) * index) / 4,
  );
  return (
    <div className="timeline-ruler">
      <span className="timeline-ruler-label">UTC</span>
      <div className="timeline-axis">
        {marks.map((time, index) => (
          <span key={time} style={{left: `${index * 25}%`}}>
            {timeLabel(time)}
          </span>
        ))}
        <input
          type="range"
          className="timeline-seek"
          aria-label="Timeline playhead"
          min={window.start}
          max={window.end}
          step={Math.max(1, (window.end - window.start) / 1000)}
          value={Math.max(window.start, Math.min(window.end, cursor))}
          aria-valuetext={`${timeLabel(cursor)} UTC`}
          onKeyDown={event => {
            const target = timelineKeyIndex(
              event.key,
              campaign.pointIndex,
              campaign.orderedMeasurements.length,
            );
            if (target === null) return;
            event.preventDefault();
            select(target);
          }}
          onChange={event => {
            const target = Number(event.target.value);
            let nearest = campaign.pointIndex;
            let distance = Number.POSITIVE_INFINITY;
            campaign.orderedMeasurements.forEach((measurement, index) => {
              const candidate = Math.abs(Date.parse(measurement.timestamp) - target);
              if (candidate < distance) {
                nearest = index;
                distance = candidate;
              }
            });
            select(nearest);
          }}
        />
        <Playhead window={window} cursor={cursor} />
      </div>
    </div>
  );
}

function timelineKeyIndex(key: string, current: number, count: number): number | null {
  switch (key) {
    case 'ArrowRight':
    case 'ArrowUp':
      return current + 1;
    case 'ArrowLeft':
    case 'ArrowDown':
      return current - 1;
    case 'Home':
      return 0;
    case 'End':
      return count - 1;
    case 'PageUp':
      return current + 10;
    case 'PageDown':
      return current - 10;
    default:
      return null;
  }
}

function TimelineTrackRow({
  track,
  window,
  cursor,
  onSelect,
  agentMode,
}: {
  readonly track: TimelineTrack;
  readonly window: TimeWindow;
  readonly cursor: number;
  readonly onSelect: (id: string) => void;
  readonly agentMode: boolean;
}): JSX.Element {
  return (
    <div className="timeline-track-row">
      {agentMode ? (
        <button
          type="button"
          className="timeline-track-name"
          title={track.label}
          onClick={() => onSelect(track.id)}
        >
          {track.label}
        </button>
      ) : (
        <span className="timeline-track-name" aria-hidden="true">
          {track.label}
        </span>
      )}
      <div className="timeline-track">
        {track.clips
          .filter(clip => clip.end >= window.start && clip.start <= window.end)
          .map(clip => {
            const left = position(clip.start, window);
            const width = Math.max(0, position(clip.end, window) - left);
            return (
              <button
                type="button"
                className={`timeline-bar outcome-${clip.state}`}
                key={clip.id}
                style={
                  {left: `min(${left}%, calc(100% - 4px))`, width: `${width}%`} as CSSProperties
                }
                aria-current={clip.selected ? 'true' : undefined}
                aria-label={`${clip.label}, ${clip.state}${clip.selected ? ', contains selected measurement' : ''}. Open ${agentMode ? 'agent' : 'workstream'}.`}
                title={`${clip.label} · ${timeLabel(clip.start)} – ${timeLabel(clip.end)} UTC`}
                onClick={() => onSelect(agentMode ? track.id : clip.id)}
              >
                <span className="timeline-bar-label">{clip.label}</span>
              </button>
            );
          })}
        <Playhead window={window} cursor={cursor} />
      </div>
    </div>
  );
}

function Playhead({
  window,
  cursor,
}: {
  readonly window: TimeWindow;
  readonly cursor: number;
}): JSX.Element | null {
  if (cursor < window.start || cursor > window.end) return null;
  return (
    <div
      className="cursor-line"
      style={{left: `${position(cursor, window)}%`}}
      aria-hidden="true"
    />
  );
}

function position(time: number, window: TimeWindow): number {
  return Math.max(0, Math.min(100, ((time - window.start) / (window.end - window.start)) * 100));
}

function workstreamTracks(
  workstreams: Workstream[],
  cursor: number,
  selectedId: string | undefined,
): TimelineTrack[] {
  const tracks: TimelineTrack[] = [];
  const ends: number[] = [];
  for (const item of [...workstreams].sort(
    (a, b) => Date.parse(a.startedAt) - Date.parse(b.startedAt),
  )) {
    const start = Date.parse(item.startedAt);
    const end = Date.parse(item.finishedAt);
    const available = ends.findIndex(value => value <= start);
    const lane = available < 0 ? tracks.length : available;
    const track = tracks[lane] ?? {
      id: `track-${lane}`,
      label: String(lane + 1).padStart(2, '0'),
      clips: [],
    };
    tracks[lane] = track;
    ends[lane] = end;
    if (start > cursor) continue;
    track.clips.push({
      id: item.id,
      label: item.title,
      start,
      end: Math.min(end, cursor),
      state: end > cursor ? 'active' : item.outcome,
      selected: item.id === selectedId,
    });
  }
  return tracks;
}

function agentTracks(scenario: CampaignRecord, cursor: number): TimelineTrack[] {
  return scenario.agents.map(agent => {
    const intervals = scenario.workstreams
      .filter(item => agent.workstreamIds.includes(item.id) && Date.parse(item.startedAt) <= cursor)
      .map(item => ({
        start: Date.parse(item.startedAt),
        end: Math.min(Date.parse(item.finishedAt), cursor),
      }))
      .sort((a, b) => a.start - b.start);
    const merged: {start: number; end: number}[] = [];
    for (const interval of intervals) {
      const previous = merged.at(-1);
      if (previous !== undefined && interval.start <= previous.end)
        previous.end = Math.max(previous.end, interval.end);
      else merged.push({...interval});
    }
    return {
      id: agent.id,
      label: agent.name,
      clips: merged.map(interval => ({
        id: `${agent.id}-${interval.start}`,
        label: agent.name,
        ...interval,
        state: 'activity',
        selected: false,
      })),
    };
  });
}

function timeLabel(timestamp: number): string {
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone: 'UTC',
  }).format(timestamp);
}
