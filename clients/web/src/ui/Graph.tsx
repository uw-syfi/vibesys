import type {AgentPhaseStatus} from '@vibesys/core-state';
import {useCallback, useLayoutEffect, useMemo, useRef, useState} from 'react';
import {layoutGraph, NODE, NODE_GAP, titleCase} from '../derive.js';
import type {AgentGraph, EdgeTone, GraphNode, PlacedEdge} from '../model.js';
import './Graph.css';

export interface GraphProps {
  round: number | null;
  graph: AgentGraph;
}

/** Which end of an axis the panel has more graph past, which that edge's fade marks. */
type More = 'start' | 'end' | 'both' | undefined;

/** Three node rows, from the layout's own card and gap, so the cap cannot drift from them. */
const CAP = NODE.height * 3 + NODE_GAP * 2;

/** Where a scroll sits on one axis: nowhere to go, or more behind, ahead, or both. */
function moreOn(room: number, at: number): More {
  if (room <= 1) return undefined;
  return at <= 1 ? 'end' : at >= room - 1 ? 'start' : 'both';
}

const TONES: EdgeTone[] = ['idle', 'done', 'live', 'failed'];

/** The live agent is "Running", the word the rail's tooltip and the live region already use. */
function statusWord(status: AgentPhaseStatus): string {
  return status === 'active' ? 'Running' : titleCase(status);
}

const path = (edge: PlacedEdge) =>
  edge.points.map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x},${point.y}`).join(' ');

/**
 * The selected round's agent graph, above the log and outside its scroller: a card per agent, laid
 * out left to right by dagre, and an arrow per handover toned by how that handover went. Adjacent
 * agents of a kind that say the same thing arrive as one card with a count. The arrows are one SVG
 * behind the cards and out of the accessibility tree; the cards are a status display and take no
 * focus. Source order is loop order whatever the layout decided.
 */
export function Graph({round, graph}: GraphProps) {
  const row = useRef<HTMLDivElement | null>(null);
  const [more, setMore] = useState<More>(undefined);
  const [down, setDown] = useState<More>(undefined);
  const layout = useMemo(() => layoutGraph(graph), [graph]);

  // Both axes: past three node rows the panel scrolls down as well as sideways, and a cut card is
  // as unreachable as a cut column. Two pieces of state, not one object, so an unchanged axis
  // re-renders nothing.
  const measure = useCallback(() => {
    const node = row.current;
    if (node === null) return;
    setMore(moreOn(node.scrollWidth - node.clientWidth, node.scrollLeft));
    setDown(moreOn(node.scrollHeight - node.clientHeight, node.scrollTop));
  }, []);

  // A callback ref, not an effect: the row exists only once a round has agents, which is later
  // than the first commit, and an effect with stable deps runs before that and never again.
  const attach = useCallback(
    (node: HTMLDivElement | null) => {
      row.current = node;
      if (node === null) return;
      const observer = new ResizeObserver(measure);
      observer.observe(node);
      return () => {
        observer.disconnect();
        row.current = null;
      };
    },
    [measure],
  );

  // The canvas grows with the agent count, and that never changes the row's own size, so the
  // observer never fires for it. A new layout is the only other thing that can.
  useLayoutEffect(() => {
    if (layout.nodes.length > 0) measure();
  }, [layout, measure]);

  if (round === null || layout.nodes.length === 0) return null;
  return (
    <section className="graph">
      <div className="col">
        {/* The scrollport is what takes focus, so it is what needs a name, and a named group is
            what it is. The rule's semantic stand-in, <fieldset>, is for form controls. */}
        {/* biome-ignore lint/a11y/useSemanticElements: <fieldset> would be wrong here. */}
        <div
          ref={attach}
          className="gflow"
          data-more={more}
          data-down={down}
          style={{maxHeight: CAP}}
          role="group"
          aria-label={`Round ${round} agents`}
          // A tab stop while it scrolls on either axis: not every browser focuses a scroll
          // container on its own, and a panel that fits has nothing for the keyboard to do.
          tabIndex={more === undefined && down === undefined ? -1 : 0}
          onScroll={measure}
        >
          <div className="gcanvas" style={{width: layout.width, height: layout.height}}>
            <svg className="gedges" width={layout.width} height={layout.height} aria-hidden="true">
              <defs>
                {TONES.map(tone => (
                  <marker
                    key={tone}
                    id={`gtip-${tone}`}
                    data-tone={tone}
                    markerWidth="6"
                    markerHeight="6"
                    refX="6"
                    refY="3"
                    orient="auto"
                    markerUnits="userSpaceOnUse"
                  >
                    <path d="M0,0 L6,3 L0,6 Z" />
                  </marker>
                ))}
              </defs>
              {layout.edges.map(edge => (
                <path
                  key={`${edge.from}>${edge.to}`}
                  className="gedge"
                  data-tone={edge.tone}
                  markerEnd={`url(#gtip-${edge.tone})`}
                  d={path(edge)}
                />
              ))}
            </svg>
            <ol className="gnodes">
              {layout.nodes.map(node => (
                <li
                  key={node.id}
                  className="gnode"
                  data-status={node.status}
                  aria-current={node.status === 'active' ? 'step' : undefined}
                  style={{left: node.x, top: node.y, width: NODE.width, height: NODE.height}}
                >
                  <span className="grole trunc">
                    {node.role}
                    {node.count > 1 ? (
                      <>
                        {/* U+00D7 is punctuation: a screen reader may drop it or say "times",
                            so the count is a word of its own for them. */}
                        <span className="sr-only">{` ${node.count} runs`}</span>
                        <span className="gn" aria-hidden="true">{` ×${node.count}`}</span>
                      </>
                    ) : null}
                  </span>
                  <span className="gmeta">
                    <span className="gstat">{statusWord(node.status)}</span>
                    <Runtime node={node} />
                  </span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
    </section>
  );
}

/**
 * The model the agent ran on, which is the half that varies between agents. When a harness is
 * recorded too the card has no room for it, so the pair goes to the tooltip and to the name a
 * screen reader announces.
 */
function Runtime({node}: {node: GraphNode}) {
  if (node.runtime === null) return null;
  if (node.runtimeTip === null) return <span className="grun mono trunc">{node.runtime}</span>;
  return (
    <>
      <span className="sr-only">{node.runtimeTip}</span>
      <span className="grun mono trunc" data-tip={node.runtimeTip} aria-hidden="true">
        {node.runtime}
      </span>
    </>
  );
}
