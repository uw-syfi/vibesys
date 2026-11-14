/**
 * The design system: domain-free React components drawn only from the design tokens. Props are data
 * and callbacks; variants are token names; no component takes `className` or `style`, so a screen
 * composed from them needs no CSS of its own. An app imports `./tokens.css`, `./fonts.css` and
 * `./components.css` once.
 */
export {applyTheme, type ThemeTarget} from './apply-theme.js';
export {ActionPanel} from './components/action-panel.js';
export {AppFrame} from './components/app-frame.js';
export {AreaChart, type ChartLayer as AreaChartLayer} from './components/area-chart.js';
export {type BarColumn, type BarFigures, type BarRow, BarRows} from './components/bar-rows.js';
export {BRAND_MARK_NAMES, BrandMark, type BrandMarkName} from './components/brand-mark.js';
export {Breadcrumb, type Crumb} from './components/breadcrumb.js';
export {Brief, type BriefField} from './components/brief.js';
export {
  Button,
  type ButtonAction,
  type ButtonFace,
  type ButtonProps,
  type ButtonSize,
  type ButtonVariant,
} from './components/button.js';
export {Card} from './components/card.js';
export {
  Chart,
  ChartColumns,
  ChartDetail,
  ChartLegend,
  type ChartLegendItem,
  type ChartLegendMark,
  type ChartMarker,
  type ChartNote,
  ChartPanel,
  type ChartPoint,
  type ChartReference,
  type ChartSize,
  type ChartTick,
  type ChartTimeAxis,
  type ChartTip,
  chartPlotMargins,
} from './components/chart.js';
export {ChatMessage} from './components/chat-message.js';
export {type CheckItem, CheckList, type CheckState} from './components/check-list.js';
export {ChoiceTiles, type TileOption} from './components/choice-tiles.js';
export {ClipRow} from './components/clip-row.js';
export {Collapsible} from './components/collapsible.js';
export {
  CellInline,
  CellLines,
  CellLink,
  CellMark,
  CellSparkline,
  ColumnList,
  type ColumnSize,
  type ListColumn,
  type RowMarks,
} from './components/column-list.js';
export {
  type ColumnTier,
  type ColumnWidths,
  clampColumnWidth,
  columnTier,
  MAX_COLUMN_WIDTH,
  MIN_COLUMN_WIDTH,
  type TrackedColumn,
  widthToShowAll,
} from './components/column-tracks.js';
export {
  Conversation,
  type ConversationAuthor,
  type ConversationEarlier,
  type ConversationItem,
} from './components/conversation.js';
export {Dialog} from './components/dialog.js';
export {
  EmptyState,
  KeyValue,
  type KeyValueItem,
  Prose,
  type ProseBlock,
  Stat,
} from './components/display.js';
export {DotLegend, type LegendItem} from './components/dot-legend.js';
export {CountedFact, Fact, FactLine, MutedFact} from './components/facts.js';
export {Field, TextInput} from './components/field.js';
export {type Figure, FigureStrip} from './components/figure-strip.js';
export {
  Gantt,
  type GanttAxis,
  type GanttFold,
  type GanttLegend,
  type GanttMark,
  type GanttRow,
  type GanttSegment,
  type GanttTick,
  type GanttView,
} from './components/gantt.js';
export type {Guide} from './components/guides.js';
export {ICON_NAMES, Icon, type IconName} from './components/icon.js';
export {type AxisLabel, labelsFit, type PlacedLabel, placeLabels} from './components/label-fit.js';
export {
  type Align,
  Dock,
  Grid,
  type GridColumn,
  type Justify,
  MainSide,
  Page,
  type PageState,
  type PageWidth,
  pageArrival,
  pageState,
  Reserve,
  Row,
  SectionLayout,
  Spacer,
  Split,
  Stack,
} from './components/layout.js';
export {type LogLine, LogPane} from './components/log-pane.js';
export {LinkOpener, Markdown} from './components/markdown.js';
export {markdownToPlain} from './components/markdown-plain.js';
export {Measure} from './components/measure.js';
export {
  Menu,
  MenuBounds,
  type MenuHandle,
  type MenuItem,
  type MenuProps,
} from './components/menu.js';
export {
  Message,
  type MessageTone,
  type MessageVariant,
  ToolCall,
  type ToolState,
} from './components/message.js';
export {Gauge, LoadingBar, Meter, Readings, ReadingsGroup} from './components/meter.js';
export {denseTilesPerRow, MetricTile, MetricTiles} from './components/metric-tile.js';
export {MiddleText} from './components/middle-text.js';
export {
  Narrative,
  Said,
  Thought,
  ToolGroup,
  type ToolLine,
} from './components/narrative.js';
export {NewBelow} from './components/new-below.js';
export {type NiceScale, niceScale} from './components/nice-scale.js';
export {
  type ChartKeyItem,
  OverviewGroup,
  OverviewTile,
  ResultChart,
  ResultChartKey,
  type SparkMark,
  type SparkPoint,
} from './components/overview-tiles.js';
export {Pane, Panes, type PaneWidth} from './components/panes.js';
export {
  type ParetoAxis,
  ParetoChart,
  type ParetoPoint,
  type ParetoSize,
  type ParetoState,
} from './components/pareto-chart.js';
export {
  type Direction,
  type Directions,
  dominates,
  frontierAt,
  paretoFrontier,
  type Tradeoff,
} from './components/pareto-frontier.js';
export {type Completion, PathInput} from './components/path-input.js';
export {withoutPaths} from './components/paths.js';
export {PickCard, PickCards} from './components/pick-card.js';
export {
  type HeaderColumn,
  type ListHeader,
  PickList,
  PickRow,
  type RowBadge,
  type RowBar,
  type RowTrail,
} from './components/pick-list.js';
export {RailFrame, RailItem} from './components/rail.js';
export {
  RangeStrip,
  type RangeWindow,
  type StripPoint,
  windowMoved,
  windowOfDrag,
} from './components/range-strip.js';
export {type Density, Screen} from './components/screen.js';
export {Scrubber, type ScrubberTick} from './components/scrubber.js';
export {SearchBox} from './components/search-box.js';
export {
  type ChoiceOption,
  SegmentedControl,
  type SegmentOption,
} from './components/segmented-control.js';
export {Select, type SelectOption} from './components/select.js';
export {ShareBar, type SharePart} from './components/share-bar.js';
export {useSideActionLayout} from './components/side-action-layout.js';
export {
  SidebarGroup,
  SidebarItem,
  SidebarSection,
  type SidebarTarget,
} from './components/sidebar.js';
export {Slider} from './components/slider.js';
export {Sparkline, type SparkStep} from './components/sparkline.js';
export {type AreaLayer, StackedArea} from './components/stacked-area.js';
export {
  type BarMark,
  type BarPlace,
  type BarSegment,
  type BarTimeAxis,
  placeBars,
  type StackedBar,
  StackedBars,
} from './components/stacked-bars.js';
export {Stale} from './components/stale.js';
export {Chip, StatusDot, type StatusTone} from './components/status.js';
export {type StepItem, StepList, type StepState} from './components/step-list.js';
export {type ReplyField, StructuredReply} from './components/structured-reply.js';
export {
  SummaryCard,
  type SummaryCardUpdate,
  type SummaryFlag,
  type SummaryResult,
  type SummaryStat,
} from './components/summary-card.js';
export {Swatch, type SwatchItem, SwatchLegend} from './components/swatch-legend.js';
export {Table, type TableColumn, type TableSort} from './components/table.js';
export {Tabs} from './components/tabs.js';
export {
  Text,
  type TextElement,
  type TextTone,
  Wordmark,
  type WordmarkSize,
  type WordmarkTone,
} from './components/text.js';
export {ThemeSwatch} from './components/theme-swatch.js';
export {Thread, type ThreadItem} from './components/thread.js';
export {
  fittedTimeTicks,
  type LabelledTick,
  timeTicks,
  type ZoneOffset,
} from './components/time-ticks.js';
export {type Stage, type StageState, Timeline} from './components/timeline.js';
export {Toast, type ToastItem, ToastStack, type ToastTone} from './components/toast.js';
export {Checkbox, Switch} from './components/toggle.js';
export {type RowChip, Tree, TreeRow} from './components/tree.js';
export {TurnResult} from './components/turn-result.js';
export {WindowFrame} from './components/window-frame.js';
export type {
  ColorRole,
  ElevationLevel,
  RadiusToken,
  SizeToken,
  SpaceToken,
  TextStyle,
  ThemeMode,
  ThemeName,
} from './tokens/index.js';
