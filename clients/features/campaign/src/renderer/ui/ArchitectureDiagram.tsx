import type {
  ArchitectureAttributionValue,
  ArchitectureComponent,
  ArchitectureGraphDocument,
} from '@vibesys/contracts';
import {type CSSProperties, type JSX, useId, useRef} from 'react';
import {
  foldArchitectureCursor,
  initialArchitectureState,
  projectArchitecture,
} from '../../state/architecture.js';
import {layoutArchitecture} from '../../state/architecture-layout.js';
import {Sheet, SheetClose, SheetContent, SheetTitle} from './components/sheet.js';

export interface ArchitectureDiagramProps {
  readonly graph: ArchitectureGraphDocument;
  readonly cursorMeasurementId: string | null;
  readonly cursorSequence: number;
  readonly selectedComponentId: string | null;
  readonly onSelectedComponentId: (id: string | null) => void;
}

/** Accessible graph projection synchronized to the campaign's existing historical cursor. */
export function ArchitectureDiagram({
  graph,
  cursorMeasurementId,
  cursorSequence,
  selectedComponentId,
  onSelectedComponentId,
}: ArchitectureDiagramProps): JSX.Element {
  const instanceId = useId();
  const opener = useRef<HTMLElement | null>(null);
  const state = foldArchitectureCursor(initialArchitectureState(graph), {
    type: 'campaign-cursor',
    cursor: {measurementId: cursorMeasurementId, sequence: cursorSequence},
  });
  const view = projectArchitecture(state);
  const layout = layoutArchitecture(graph);
  const components = new Map(graph.components.map(component => [component.id, component]));
  const selectedComponent = components.get(selectedComponentId ?? '');
  const titleId = architectureTitleId(instanceId);
  return (
    <>
      <section className="panel architecture-panel" aria-labelledby={titleId}>
        <header className="architecture-header">
          <div>
            <p className="section-kicker">SYSTEM TOPOLOGY · {graph.generation.approach}</p>
            <h2 id={titleId}>{graph.title}</h2>
            <p>{graph.description}</p>
          </div>
          <div className="architecture-observation" aria-live="polite">
            <span>Architecture capture</span>
            <strong>{view.observation?.label ?? 'Not yet available'}</strong>
          </div>
        </header>
        <section
          className="architecture-scroll"
          aria-label="Scrollable system architecture graph"
          // biome-ignore lint/a11y/noNoninteractiveTabindex: This named overflow viewport must receive focus for keyboard panning; focusing each graph button does not give arrow keys to the viewport, while a textarea or details element would invent editing or disclosure semantics.
          tabIndex={0}
        >
          <div
            className="architecture-canvas"
            style={{width: layout.width, height: layout.height} as CSSProperties}
          >
            <svg
              className="architecture-edges"
              width={layout.width}
              height={layout.height}
              viewBox={`0 0 ${layout.width} ${layout.height}`}
              aria-hidden="true"
            >
              {layout.edges.map(edge => (
                <path key={edge.id} d={edge.path} />
              ))}
            </svg>
            {layout.nodes.map(node => {
              const component = components.get(node.id);
              if (component === undefined) return null;
              const attribution = view.attributions.get(node.id);
              const changed = view.changedComponentIds.has(node.id);
              return (
                <article
                  key={node.id}
                  className={`architecture-node${changed ? ' architecture-node-changed' : ''}`}
                  data-component-id={node.id}
                  data-changed={changed ? 'true' : 'false'}
                  style={
                    {
                      left: node.x,
                      top: node.y,
                      width: node.width,
                      minHeight: node.height,
                    } as CSSProperties
                  }
                >
                  <button
                    id={componentTriggerId(instanceId, node.id)}
                    type="button"
                    className="architecture-node-trigger"
                    aria-controls={componentDetailsId(instanceId, node.id)}
                    aria-expanded={selectedComponentId === node.id}
                    aria-haspopup="dialog"
                    aria-label={`${component.label}, ${component.kind}${
                      changed ? ', changed at selected measurement' : ''
                    }. Open component details.`}
                    onClick={event => {
                      opener.current = event.currentTarget;
                      onSelectedComponentId(node.id);
                    }}
                  >
                    <span className="architecture-node-heading">
                      <span>{component.kind}</span>
                      {changed && <b>Changed</b>}
                    </span>
                    <strong>{component.label}</strong>
                    <span className="architecture-node-description">{component.description}</span>
                  </button>
                  <dl>
                    <Metric label="Cost" value={attribution?.cost} />
                    <Metric label="Latency" value={attribution?.latency} />
                  </dl>
                </article>
              );
            })}
          </div>
        </section>
        <ul className="sr-only" aria-label="Architecture relationships">
          {graph.edges.map(edge => (
            <li key={edge.id}>
              {components.get(edge.source_id)?.label} {edge.label ?? edge.kind}{' '}
              {components.get(edge.target_id)?.label}
            </li>
          ))}
        </ul>
        <div className="architecture-changes">
          <strong>Changed files at this capture</strong>
          {view.observation === null ? (
            <span>No architecture capture is available at this campaign position.</span>
          ) : view.changedFiles.length === 0 ? (
            <span>No changed files were recorded for this capture.</span>
          ) : (
            <ul>
              {view.changedFiles.map(file => (
                <li key={file}>
                  <code>{file}</code>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
      <ArchitectureComponentSheet
        component={selectedComponent}
        instanceId={instanceId}
        attribution={selectedComponent && view.attributions.get(selectedComponent.id)}
        changedFiles={
          selectedComponent !== undefined && view.changedComponentIds.has(selectedComponent.id)
            ? (view.observation?.changes.find(
                change => change.component_id === selectedComponent.id,
              )?.changed_files ?? [])
            : []
        }
        onClose={() => closeComponentDetails(opener.current, onSelectedComponentId)}
      />
    </>
  );
}

function ArchitectureComponentSheet({
  component,
  instanceId,
  attribution,
  changedFiles,
  onClose,
}: {
  readonly component: ArchitectureComponent | undefined;
  readonly instanceId: string;
  readonly attribution:
    | {
        readonly cost: ArchitectureAttributionValue | null;
        readonly latency: ArchitectureAttributionValue | null;
      }
    | undefined;
  readonly changedFiles: readonly string[];
  readonly onClose: () => void;
}): JSX.Element {
  return (
    <Sheet open={component !== undefined} onOpenChange={open => !open && onClose()}>
      {component !== undefined && (
        <SheetContent
          id={componentDetailsId(instanceId, component.id)}
          className="architecture-detail-sheet"
          onCloseAutoFocus={event => event.preventDefault()}
        >
          <header className="dialog-head">
            <div>
              <p className="section-kicker">{component.kind} COMPONENT</p>
              <SheetTitle>{component.label}</SheetTitle>
            </div>
            <SheetClose asChild>
              <button type="button" className="dialog-close" aria-label="Close component details">
                ×
              </button>
            </SheetClose>
          </header>
          <p className="dialog-hypothesis">{component.description}</p>
          <dl className="architecture-detail-facts">
            <div>
              <dt>Stable identity</dt>
              <dd>
                <code>{component.id}</code>
              </dd>
            </div>
            <div>
              <dt>{component.anchor.kind}</dt>
              <dd>
                <code>{component.anchor.value}</code>
              </dd>
            </div>
            <Metric label="Cost" value={attribution?.cost} />
            <Metric label="Latency" value={attribution?.latency} />
          </dl>
          <section
            className="architecture-detail-changes"
            aria-labelledby={componentFilesTitleId(instanceId, component.id)}
          >
            <h3 id={componentFilesTitleId(instanceId, component.id)}>
              Changed files at this capture
            </h3>
            {changedFiles.length === 0 ? (
              <p>No changed files were recorded for this component.</p>
            ) : (
              <ul>
                {changedFiles.map(file => (
                  <li key={file}>
                    <code>{file}</code>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </SheetContent>
      )}
    </Sheet>
  );
}

function architectureTitleId(instanceId: string): string {
  return `${instanceId}-architecture-title`;
}

function componentDetailsId(instanceId: string, componentId: string): string {
  return `${instanceId}-architecture-component-${encodeURIComponent(componentId)}`;
}

function componentTriggerId(instanceId: string, componentId: string): string {
  return `${instanceId}-architecture-component-trigger-${encodeURIComponent(componentId)}`;
}

function componentFilesTitleId(instanceId: string, componentId: string): string {
  return `${instanceId}-architecture-component-files-${encodeURIComponent(componentId)}`;
}

function closeComponentDetails(
  opener: HTMLElement | null,
  onSelectedComponentId: (id: string | null) => void,
): void {
  onSelectedComponentId(null);
  queueMicrotask(() => {
    if (opener?.isConnected === true) opener.focus();
  });
}

function Metric({
  label,
  value,
}: {
  readonly label: string;
  readonly value: ArchitectureAttributionValue | null | undefined;
}): JSX.Element {
  return (
    <div title={value?.provenance}>
      <dt>{label}</dt>
      <dd>{value === null || value === undefined ? 'Unavailable' : formatValue(value)}</dd>
    </div>
  );
}

function formatValue(value: ArchitectureAttributionValue): string {
  return `${new Intl.NumberFormat('en-US', {maximumFractionDigits: 3}).format(value.value)} ${
    value.unit
  }`;
}
