package attachview

import (
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// This file is the whole logic of `run attach`: what is known, which agent is being looked at, and
// what to print for each thing that happens. It is a pure function from the model and an input to a
// new model and outputs, and it knows nothing of terminals, streams, clocks or goroutines: the shell
// in converse.go feeds it inputs and carries out its outputs.
//
// Data flows one way. Inputs are the frames of the two streams (the run's, and the viewed agent's),
// keys, resizes, ticks, and the end of the caller. Outputs are lines to print, a footer, a screen
// clear, a stream to open, and the end. Everything printed passes emit, which makes the text safe.

// Options say how the view starts and what it shows.
type Options struct {
	// Controls says the view takes keys and keeps a footer. The shell decides it; the model only
	// reads it, so nothing here knows what a terminal is.
	Controls bool
	// Start is the id of the agent to start in, or empty for the run's lead.
	Start string
	// Only says the view follows Start alone and ends with it, as `--agent` does without keys.
	Only bool
	// History is how many of an agent's newest items a replay holds, or zero for all of them.
	History int
	Zone    *time.Location
	// Now is the time at the start, the clock of the footer until the first Tick.
	Now           time.Time
	Width, Height int
}

// DefaultHistory is how many items a replay holds unless asked for more.
const DefaultHistory = 200

// Model is what the view knows. It owns its View and folds frames into it in place, so a Model must
// not be used again after it has been passed to Update.
type Model struct {
	opt           Options
	width, height int
	now           time.Time
	View          *View
	// log is every change of the run seen so far, oldest first, so that a view can be replayed.
	log []Change

	agent string
	// gen counts the subscriptions. An agent frame carries the one it was read under, and a frame of
	// any other is dropped by Update, so a late frame of an old view cannot print in the new one.
	gen            uint64
	hist           bool
	subscribedOver bool
	swept          bool

	seen     map[string]bool
	turns    map[string]*pb.Turn
	tools    map[string]int
	closed   map[string]bool
	openTurn string
	// lastTurn is the number of the newest turn shown.
	lastTurn uint32

	picker *picker
	footer string
	done   bool
}

type picker struct {
	agents []string
	typed  string
}

// Input is something that happened. The set is closed: RunFrame, AgentHistory, AgentFrame,
// AgentSweep, Key, Resize, Tick, StreamFailed and Detach.
type Input interface{ isInput() }

// RunFrame is a frame of the run stream, classified: one of the three is set.
type RunFrame struct {
	Snapshot *pb.RunsSnapshot
	Change   *pb.RunChange
	Search   *pb.SearchState
}

// AgentHistory is the replay of the viewed agent: its newest items, oldest first.
type AgentHistory struct {
	Gen   uint64
	Items []*pb.Item
}

// AgentFrame is a live frame of the viewed agent: an item or a turn.
type AgentFrame struct {
	Gen  uint64
	Item *pb.Item
	Turn *pb.Turn
}

// AgentSweep is the newest items of the viewed agent, read to catch up with what the run's own stream
// said had happened: when a turn of the agent ended, and once the run is over. Final marks the read
// that was asked for after the run was over.
type AgentSweep struct {
	Gen   uint64
	Final bool
	Items []*pb.Item
}

// Resize says the terminal now has this size.
type Resize struct{ Width, Height int }

// Tick says what time it is now.
type Tick struct{ Now time.Time }

// StreamFailed says a stream or a read ended with an error. Gen is the subscription it belongs to,
// or zero for the run's stream.
type StreamFailed struct {
	Gen uint64
	Err error
}

// Detach says the caller is leaving.
type Detach struct{}

func (RunFrame) isInput()     {}
func (AgentHistory) isInput() {}
func (AgentFrame) isInput()   {}
func (AgentSweep) isInput()   {}
func (Key) isInput()          {}
func (Resize) isInput()       {}
func (Tick) isInput()         {}
func (StreamFailed) isInput() {}
func (Detach) isInput()       {}

// Output is something to do. The set is closed: Print, Clear, SetFooter, Subscribe, Sweep and Exit.
type Output interface{ isOutput() }

// Print writes lines. Each is one line of meaning, free of control characters and line breaks, that
// Render turns into what is drawn.
type Print struct{ Lines []Line }

// Clear empties the screen.
type Clear struct{}

// SetFooter replaces the footer line. Empty text removes it.
type SetFooter struct{ Text string }

// Subscribe ends the subscription to any agent and opens one to Agent, whose frames are tagged Gen.
// Limit is how many of its newest items the replay holds, or zero for all.
type Subscribe struct {
	Agent string
	Gen   uint64
	Limit int
}

// Sweep asks for the newest items of Agent, to come back as an AgentSweep of Gen.
type Sweep struct {
	Agent string
	Gen   uint64
	Final bool
}

// ExitCause says why the view ended.
type ExitCause int

// The ways the view ends.
const (
	// ExitEnded: what the view followed is over.
	ExitEnded ExitCause = iota
	// ExitDetached: the caller left; the run goes on.
	ExitDetached
	// ExitBroke: a stream failed; Err says how.
	ExitBroke
)

// Exit ends the view. Nothing follows it.
type Exit struct {
	Cause ExitCause
	Err   error
}

func (Print) isOutput()     {}
func (Clear) isOutput()     {}
func (SetFooter) isOutput() {}
func (Subscribe) isOutput() {}
func (Sweep) isOutput()     {}
func (Exit) isOutput()      {}

// Begin starts the view of a run that has a view of its history, which is what the snapshot showed.
func Begin(opt Options, view *View, history []Change) (Model, []Output) {
	m := Model{opt: opt, width: opt.Width, height: opt.Height, now: opt.Now, View: view, log: history}
	m.agent = opt.Start
	if m.agent == "" {
		m.agent = view.Run.GetLeadAgentId()
	}
	var outs []Output
	if m.agent != m.lead() {
		outs = append(outs, Print{Lines: m.emit(m.header())})
	}
	outs = append(outs, m.subscribe())
	return m.finish(outs)
}

// Update is the one step of the view.
func Update(m Model, in Input) (Model, []Output) {
	if m.done {
		return m, nil
	}
	var outs []Output
	switch in := in.(type) {
	case RunFrame:
		outs = m.runFrame(in)
	case AgentHistory:
		outs = m.history(in)
	case AgentFrame:
		outs = m.live(in)
	case AgentSweep:
		outs = m.sweep(in)
	case Key:
		outs = m.key(in)
	case Resize:
		m.width, m.height = in.Width, in.Height
	case Tick:
		m.now = in.Now
	case StreamFailed:
		if in.Gen == 0 || in.Gen == m.gen {
			outs = m.exit(ExitBroke, in.Err)
		}
	case Detach:
		outs = m.exit(ExitDetached, nil)
	}
	return m.finish(outs)
}

// finish ends a step: it asks whether the view is over, and sets the footer.
func (m Model) finish(outs []Output) (Model, []Output) {
	if !m.done {
		outs = append(outs, m.checkOver()...)
	}
	if !m.done {
		if text := m.footerText(); text != m.footer {
			m.footer = text
			outs = append(outs, SetFooter{Text: text})
		}
	}
	return m, outs
}

func (m *Model) exit(cause ExitCause, err error) []Output {
	m.done = true
	return []Output{Exit{Cause: cause, Err: err}}
}

// Viewing is the id of the agent whose conversation is shown.
func (m Model) Viewing() string { return m.agent }

// Done says the view has ended: it takes no more input.
func (m Model) Done() bool { return m.done }

// Replayed says the viewed agent's replay has been shown.
func (m Model) Replayed() bool { return m.hist }

func (m *Model) lead() string { return m.View.Run.GetLeadAgentId() }

// emit makes lines safe to print: no control characters, and no line break inside a line. Everything
// the view prints passes it, so a line that came from an agent cannot drive a terminal.
func (m *Model) emit(lines []Line) []Line {
	out := make([]Line, 0, len(lines))
	for _, line := range lines {
		line.Label = strings.ReplaceAll(Sanitize(line.Label), "\n", " ")
		for _, text := range strings.Split(Sanitize(line.Text), "\n") {
			out = append(out, Line{Kind: line.Kind, Label: line.Label, Text: text})
		}
	}
	return out
}

// --- the run's stream ---

func (m *Model) runFrame(in RunFrame) []Output {
	changes := m.View.Fold(in.Snapshot, in.Change, in.Search)
	m.log = append(m.log, changes...)
	if !m.hist {
		return nil // the replay will show them, in their place.
	}
	var lines []Line
	reconcile := false
	for _, c := range changes {
		if line, ok := m.changeLine(c); ok {
			lines = append(lines, line)
		}
		// The run's stream and the agent's are two streams: the run's may say a turn of the viewed agent
		// ended before the agent's does, or when the agent's page was already past it. Read the agent's
		// newest items to show the turn's last items and close it.
		reconcile = reconcile || (c.Kind == TurnEnded && c.Turn.GetAgentId() == m.agent && !m.closed[c.Turn.GetId()])
	}
	outs := m.print(lines)
	if reconcile {
		outs = append(outs, Sweep{Agent: m.agent, Gen: m.gen})
	}
	return outs
}

func (m *Model) print(lines []Line) []Output {
	if lines = m.emit(lines); len(lines) == 0 {
		return nil
	}
	return []Output{Print{Lines: lines}}
}

// changeLine is the line for a change of the run, or false when the view does not show it. The lead's
// view shows every change except the turns of the lead, which its own stream says in order with its
// items. The view of another agent shows its own changes and the run's.
func (m *Model) changeLine(c Change) (Line, bool) {
	if !m.shows(c) || strings.TrimSpace(m.View.text(c)) == "" {
		return Line{}, false
	}
	return Line{Kind: LineMilestone, Text: m.View.Describe(c, m.opt.Zone)}, true
}

func (m *Model) shows(c Change) bool {
	if c.Kind == TurnEnded && c.Turn.GetAgentId() == m.agent {
		return false
	}
	if m.agent == m.lead() {
		return true
	}
	switch c.Kind {
	case RunMoved, RunStalled, RunResumed:
		return true
	case AgentStarted, AgentEnded:
		return c.Agent.GetId() == m.agent
	case HandoffPosted, ReportPosted:
		return c.Message.GetFromAgentId() == m.agent
	case MessageRejected, ReminderSent:
		return c.Message.GetToAgentId() == m.agent
	}
	return false
}

// --- the viewed agent's conversation ---

type entry struct {
	at   time.Time
	line Line
}

func (m *Model) history(in AgentHistory) []Output {
	if in.Gen != m.gen || m.hist {
		return nil
	}
	m.hist = true
	items := slices.Clone(in.Items)
	slices.SortStableFunc(items, func(a, b *pb.Item) int { return int(a.GetOrdinal()) - int(b.GetOrdinal()) })
	var lines []entry
	skipped := 0
	if len(items) > 0 {
		skipped = int(items[0].GetOrdinal()) - 1
	}
	var floor uint32
	if skipped > 0 {
		floor = m.turn(items[0].GetTurnId()).GetOrdinal()
	}
	lines = append(lines, m.catchUp(items, floor)...)
	var changes []entry
	var from time.Time
	if skipped > 0 && len(items) > 0 {
		from = items[0].GetAt().AsTime()
	}
	for _, c := range m.log {
		if line, ok := m.changeLine(c); ok && !c.At.Before(from) {
			changes = append(changes, entry{c.At, line})
		}
	}
	if len(items) == 0 && len(changes) > DefaultHistory {
		changes = changes[len(changes)-DefaultHistory:]
	}
	var out []Line
	if skipped > 0 {
		out = append(out, Line{Kind: LineNotice, Text: fmt.Sprintf("(%d earlier items of %s are not shown; --full-history shows them all)", skipped, m.View.AgentName(m.agent))})
	}
	return m.print(append(out, merge(changes, lines)...))
}

// merge interleaves two lists that are each in order, by time, keeping the order inside each. On a
// tie the change comes first.
func merge(changes, items []entry) []Line {
	out := make([]Line, 0, len(changes)+len(items))
	i, j := 0, 0
	for i < len(changes) || j < len(items) {
		if j == len(items) || (i < len(changes) && !changes[i].at.After(items[j].at)) {
			out = append(out, changes[i].line)
			i++
		} else {
			out = append(out, items[j].line)
			j++
		}
	}
	return out
}

func (m *Model) live(in AgentFrame) []Output {
	if in.Gen != m.gen || !m.hist {
		return nil
	}
	var ents []entry
	switch {
	case in.Item != nil:
		ents = m.itemEntries(in.Item)
	case in.Turn != nil:
		ents = m.turnFrame(in.Turn)
	}
	return m.print(texts(ents))
}

func texts(ents []entry) []Line {
	out := make([]Line, len(ents))
	for i, e := range ents {
		out[i] = e.line
	}
	return out
}

// itemEntries is what a finished item adds, once: the turn lines it opens, and its own. An item that
// is still running adds nothing and will be shown when it is finished.
func (m *Model) itemEntries(it *pb.Item) []entry {
	if m.seen[it.GetId()] || it.GetStatus() == pb.ItemStatus_ITEM_STATUS_RUNNING || it.GetAgentId() != m.agent {
		return nil
	}
	m.seen[it.GetId()] = true
	ents := m.enter(it.GetTurnId(), it.GetAt().AsTime())
	if it.GetTool() != nil {
		m.tools[it.GetTurnId()]++
	}
	trigger := pb.TurnTrigger_TURN_TRIGGER_UNSPECIFIED
	if t := m.turn(it.GetTurnId()); t != nil {
		trigger = t.GetTrigger()
	}
	for _, line := range itemLines(it, promptLabel(trigger)) {
		ents = append(ents, entry{it.GetAt().AsTime(), line})
	}
	return ents
}

// turn is the newest record of a turn: the agent's own stream is ahead of the run's, except that a
// record that has ended is final.
func (m *Model) turn(id string) *pb.Turn {
	own, run := m.turns[id], m.View.Turns[id]
	if own != nil && own.GetStatus() != pb.TurnStatus_TURN_STATUS_RUNNING {
		return own
	}
	if run != nil {
		return run
	}
	return own
}

// enter opens a turn if it is not the open one, closing the one before it.
func (m *Model) enter(id string, at time.Time) []entry {
	if id == m.openTurn || m.closed[id] {
		return nil
	}
	ents := m.closeTurn(m.openTurn)
	m.openTurn = id
	name := m.View.AgentName(m.agent)
	if t := m.turn(id); t != nil {
		m.lastTurn = max(m.lastTurn, t.GetOrdinal())
		return append(ents, entry{t.GetStartedAt().AsTime(), Line{Kind: LineTurn, Text: Clock(t.GetStartedAt().AsTime(), m.opt.Zone) + "  " + fmt.Sprintf("%s turn %d started: %s", name, t.GetOrdinal(), m.View.triggerWords(t))}})
	}
	return append(ents, entry{at, Line{Kind: LineTurn, Text: Clock(at, m.opt.Zone) + "  " + name + " turn started"}})
}

// closeTurn says how a turn ended, once. A turn the view cannot yet say has ended is left unsaid, and
// not said later out of place.
func (m *Model) closeTurn(id string) []entry {
	if id == "" || m.closed[id] {
		return nil
	}
	m.closed[id] = true
	if m.openTurn == id {
		m.openTurn = ""
	}
	t := m.turn(id)
	if t == nil || t.GetStatus() == pb.TurnStatus_TURN_STATUS_RUNNING {
		return nil
	}
	at := pick(t.GetEndedAt(), t.GetStartedAt().AsTime())
	return []entry{{at, Line{Kind: LineTurn, Text: Clock(at, m.opt.Zone) + "  " + turnEndWords(m.View.AgentName(m.agent), t, m.tools[id])}}}
}

// catchUp shows what the viewed agent did that has not been shown: its turns from number floor on, as
// the run's stream knows them, each with its items among those given. A turn that made no item still
// has its lines. The last turn is closed if it has ended.
func (m *Model) catchUp(items []*pb.Item, floor uint32) []entry {
	var turns []*pb.Turn
	for _, t := range m.View.Turns {
		if t.GetAgentId() == m.agent && t.GetOrdinal() >= floor {
			turns = append(turns, t)
		}
	}
	slices.SortFunc(turns, func(a, b *pb.Turn) int { return int(a.GetOrdinal()) - int(b.GetOrdinal()) })
	byTurn := map[string][]*pb.Item{}
	for _, it := range items {
		byTurn[it.GetTurnId()] = append(byTurn[it.GetTurnId()], it)
	}
	var ents []entry
	for _, t := range turns {
		ents = append(ents, m.enter(t.GetId(), t.GetStartedAt().AsTime())...)
		for _, it := range byTurn[t.GetId()] {
			ents = append(ents, m.itemEntries(it)...)
		}
		delete(byTurn, t.GetId())
	}
	for _, it := range items { // items of turns the run's stream does not know.
		if _, left := byTurn[it.GetTurnId()]; left {
			ents = append(ents, m.itemEntries(it)...)
		}
	}
	return append(ents, m.settle()...)
}

// settle closes the open turn if the run's stream says it ended, once the replay has been read.
func (m *Model) settle() []entry {
	if t := m.turn(m.openTurn); t != nil && t.GetStatus() != pb.TurnStatus_TURN_STATUS_RUNNING {
		return m.closeTurn(m.openTurn)
	}
	return nil
}

func (m *Model) turnFrame(t *pb.Turn) []entry {
	if t.GetAgentId() != m.agent || m.closed[t.GetId()] {
		return nil
	}
	m.turns[t.GetId()] = t
	ents := m.enter(t.GetId(), t.GetStartedAt().AsTime())
	if t.GetStatus() != pb.TurnStatus_TURN_STATUS_RUNNING {
		ents = append(ents, m.closeTurn(t.GetId())...)
	}
	return ents
}

// --- the end ---

// finished says what the view follows is over: the run, or with Only the agent.
func (m *Model) finished() bool {
	if m.View.Over() {
		return true
	}
	if !m.opt.Only {
		return false
	}
	a := m.View.Agents[m.agent]
	return a != nil && agentOver(a)
}

// checkOver ends the view once what it follows is over and its conversation has been read to the end.
// What the run's stream says happened may have come before the agent's stream said so, so a view that
// was not opened after the end reads the newest items once more, and ends when they are shown.
func (m *Model) checkOver() []Output {
	if !m.finished() || !m.hist {
		return nil
	}
	if m.subscribedOver {
		return m.exit(ExitEnded, nil)
	}
	if m.swept {
		return nil
	}
	m.swept = true
	return []Output{Sweep{Agent: m.agent, Gen: m.gen, Final: true}}
}

func (m *Model) sweep(in AgentSweep) []Output {
	if in.Gen != m.gen || !m.hist {
		return nil
	}
	items := slices.Clone(in.Items)
	slices.SortStableFunc(items, func(a, b *pb.Item) int { return int(a.GetOrdinal()) - int(b.GetOrdinal()) })
	ents := m.catchUp(items, m.lastTurn)
	outs := m.print(texts(ents))
	if in.Final {
		outs = append(outs, m.exit(ExitEnded, nil)...)
	}
	return outs
}

// --- switching ---

func (m *Model) subscribe() Output {
	m.gen++
	m.hist, m.swept = false, false
	m.subscribedOver = m.finished()
	m.seen, m.turns, m.tools, m.closed, m.openTurn = map[string]bool{}, map[string]*pb.Turn{}, map[string]int{}, map[string]bool{}, ""
	m.lastTurn = 0
	return Subscribe{Agent: m.agent, Gen: m.gen, Limit: m.opt.History}
}

// switchTo clears the screen and opens another agent's view.
func (m *Model) switchTo(id string) []Output {
	m.picker = nil
	if id == m.agent {
		return nil
	}
	m.agent = id
	outs := []Output{Clear{}, Print{Lines: m.emit(m.header())}}
	return append(outs, m.subscribe())
}

func (m *Model) header() []Line {
	a := m.View.Agents[m.agent]
	parent := ""
	if p := a.GetParentAgentId(); p != "" {
		parent = ", started by " + m.View.AgentName(p)
	}
	return []Line{{Kind: LineHeader, Text: fmt.Sprintf("-- %s: %s, %s%s (id %s) --", m.View.AgentName(m.agent), StatusWord(a.GetRole().String(), "AGENT_ROLE_"), StatusWord(a.GetStatus().String(), "AGENT_STATUS_"), parent, m.agent)}}
}

// up is the view of the parent of the viewed agent: the agent that started it, else the lead.
func (m *Model) up() []Output {
	if m.agent == m.lead() {
		return nil
	}
	parent := m.View.Agents[m.agent].GetParentAgentId()
	if parent == "" || parent == m.agent || m.View.Agents[parent] == nil {
		parent = m.lead()
	}
	return m.switchTo(parent)
}

// --- keys ---

func (m *Model) key(k Key) []Output {
	if !m.opt.Controls {
		return nil
	}
	if k.Kind == KeyCtrlC {
		return m.exit(ExitDetached, nil)
	}
	if m.picker != nil {
		return m.pickerKey(k)
	}
	switch {
	case k.Kind == KeyRune && k.Rune == 'a':
		return m.openPicker()
	case k.Kind == KeyEsc, k.Kind == KeyRune && (k.Rune == 'b' || k.Rune == 'B'):
		return m.up()
	}
	return nil
}

func (m *Model) openPicker() []Output {
	agents := m.View.AgentsInOrder()
	p := &picker{}
	lines := []Line{{Kind: LineNotice, Text: "agents (type a number, Enter to open it, Esc to cancel):"}}
	for i, a := range agents {
		p.agents = append(p.agents, a.GetId())
		here := ""
		if a.GetId() == m.agent {
			here = "  <- here"
		}
		lines = append(lines, Line{Kind: LinePicker, Text: fmt.Sprintf("  %2d  %-12s %-9s %-8s %s%s", i+1, m.View.AgentName(a.GetId()), StatusWord(a.GetRole().String(), "AGENT_ROLE_"), StatusWord(a.GetStatus().String(), "AGENT_STATUS_"), a.GetId(), here)})
	}
	m.picker = p
	return m.print(lines)
}

func (m *Model) pickerKey(k Key) []Output {
	p := m.picker
	switch {
	case k.Kind == KeyEsc, k.Kind == KeyRune && k.Rune == 'a':
		m.picker = nil
	case k.Kind == KeyBackspace:
		if p.typed != "" {
			p.typed = p.typed[:len(p.typed)-1]
		}
	case k.Kind == KeyRune && k.Rune >= '0' && k.Rune <= '9':
		p.typed += string(k.Rune)
		if n, _ := strconv.Atoi(p.typed); n*10 > len(p.agents) {
			return m.pick()
		}
	case k.Kind == KeyEnter:
		return m.pick()
	}
	return nil
}

func (m *Model) pick() []Output {
	p := m.picker
	n, err := strconv.Atoi(p.typed)
	if err != nil || n < 1 || n > len(p.agents) {
		m.picker = nil
		return m.print([]Line{{Kind: LineNotice, Text: fmt.Sprintf("there is no agent %q; press a to list them", p.typed)}})
	}
	return m.switchTo(p.agents[n-1])
}

// --- the footer ---

// footerText is the status line, cut to the width, or empty without controls.
func (m *Model) footerText() string {
	if !m.opt.Controls {
		return ""
	}
	if m.picker != nil {
		return m.fit(fmt.Sprintf("agent number: %s_   (Enter opens it, Esc cancels)", m.picker.typed))
	}
	active, cost, priced := 0, 0.0, false
	for _, a := range m.View.Agents {
		if a.GetStatus() == pb.AgentStatus_AGENT_STATUS_ACTIVE {
			active++
		}
		if u := a.GetUsage(); u != nil && u.CostUsd != nil {
			cost, priced = cost+u.GetCostUsd(), true
		}
	}
	parts := []string{elapsedWords(m.elapsed()), fmt.Sprintf("%d active", active)}
	if priced {
		parts = append(parts, fmt.Sprintf("$%.2f", cost))
	}
	parts = append(parts, "viewing "+m.View.AgentName(m.agent))
	keys := "a agents"
	if m.agent != m.lead() {
		keys += ", Esc back"
	}
	return m.fit(strings.Join(parts, " | ") + " | " + keys + ", Ctrl-C detaches")
}

// fit cuts a footer to one less than the width, so that it never wraps.
func (m *Model) fit(text string) string {
	if m.width <= 0 {
		return text
	}
	return Clip(text, max(m.width-1, 1))
}

func (m *Model) elapsed() time.Duration {
	start := m.View.Run.GetCreatedAt().AsTime()
	end := m.now
	if m.View.Over() && m.View.Run.GetEndedAt() != nil {
		end = m.View.Run.GetEndedAt().AsTime()
	}
	if m.View.Run.GetCreatedAt() == nil || end.Before(start) {
		return 0
	}
	return end.Sub(start)
}

func elapsedWords(d time.Duration) string {
	d = d.Round(time.Second)
	h, mm, s := int(d.Hours()), int(d.Minutes())%60, int(d.Seconds())%60
	if h > 0 {
		return fmt.Sprintf("%dh%02dm", h, mm)
	}
	return fmt.Sprintf("%dm%02ds", mm, s)
}
