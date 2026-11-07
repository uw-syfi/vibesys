package daemon_test

import (
	"context"
	"math/rand/v2"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// scripted is a lead whose next turn the test writes itself, one event at a time.
type scripted struct {
	rig     *daemontest.Rig
	lead    core.Agent
	session *daemontest.Session
	turn    string
}

// openTurn holds the lead's session and sends it a message, so that a turn is open and stays open
// until the test ends it.
func openTurn(t *testing.T, rig *daemontest.Rig) *scripted {
	t.Helper()
	_, lead := beginRun(t, rig)
	session := rig.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})
	session.Held = true
	if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
		Id: "tell", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "go"}}}}); err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	var turn string
	for _, c := range session.Commands() {
		if start := c.GetStartTurn(); start != nil {
			turn = start.GetTurnId()
		}
	}
	if turn == "" {
		t.Fatal("the lead was never told to start a turn")
	}
	return &scripted{rig: rig, lead: lead, session: session, turn: turn}
}

// emit sends the lead's session event, lets the daemon take it, and then lets `after` pass.
func (s *scripted) emit(t *testing.T, after time.Duration, ev *pb.SessionEvent) {
	t.Helper()
	s.session.EmitEvents(ev)
	s.rig.Settle(t)
	s.rig.Clock.Advance(after)
}

func (s *scripted) item(id string, status pb.ItemStatus, body func(*pb.SessionItem)) *pb.SessionEvent {
	item := &pb.SessionItem{Id: id, TurnId: s.turn, Status: status}
	body(item)
	return &pb.SessionEvent{Body: &pb.SessionEvent_Item{Item: item}}
}

func reasoning(text string) func(*pb.SessionItem) {
	return func(i *pb.SessionItem) { i.Body = &pb.SessionItem_Reasoning{Reasoning: &pb.Text{Text: text}} }
}

func assistant(text string) func(*pb.SessionItem) {
	return func(i *pb.SessionItem) { i.Body = &pb.SessionItem_Assistant{Assistant: &pb.Text{Text: text}} }
}

func prompt(text string) func(*pb.SessionItem) {
	return func(i *pb.SessionItem) { i.Body = &pb.SessionItem_Prompt{Prompt: &pb.Text{Text: text}} }
}

func shell(command, output string) func(*pb.SessionItem) {
	return func(i *pb.SessionItem) {
		i.Body = &pb.SessionItem_Tool{Tool: &pb.ToolCall{
			Call: &pb.ToolCall_Shell{Shell: &pb.ShellCall{Command: command}}, Output: output,
		}}
	}
}

func (s *scripted) end(t *testing.T) {
	t.Helper()
	s.emit(t, 0, &pb.SessionEvent{Body: &pb.SessionEvent_TurnEnded{TurnEnded: &pb.TurnEnded{
		TurnId: s.turn, Status: pb.TurnStatus_TURN_STATUS_COMPLETED,
	}}})
}

// newest is the lead's newest page of items, as a watcher is first given it.
func (s *scripted) newest(t *testing.T) *pb.ItemsPage {
	t.Helper()
	w, first, err := s.rig.Service.WatchAgentFrom(s.lead.ID, 1000, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	return first[0].GetPage()
}

func byID(page *pb.ItemsPage, id string) *pb.Item {
	for _, it := range page.GetItems() {
		if it.GetId() == id {
			return it
		}
	}
	return nil
}

// An item remembers when it was first seen: an update changes when it was last updated, and the end
// of its turn, which finishes whatever is still running, does the same.
func TestAnItemKeepsWhenItWasFirstSeenThroughUpdatesAndTheEndOfItsTurn(t *testing.T) {
	rig := daemontest.NewRig(t)
	s := openTurn(t, rig)
	first := rig.Clock.Now()
	s.emit(t, 5*time.Second, s.item("tool-a", pb.ItemStatus_ITEM_STATUS_RUNNING, shell("make", "")))
	s.emit(t, 2*time.Second, s.item("tool-b", pb.ItemStatus_ITEM_STATUS_RUNNING, shell("sleep 9", "")))
	updated := rig.Clock.Now()
	s.emit(t, 3*time.Second, s.item("tool-a", pb.ItemStatus_ITEM_STATUS_FINISHED, shell("make", "built")))
	ended := rig.Clock.Now()
	s.end(t)
	page := s.newest(t)
	a, b := byID(page, "tool-a"), byID(page, "tool-b")
	if a == nil || b == nil {
		t.Fatalf("items missing from %v", page)
	}
	if got := a.GetStartedAt().AsTime(); !got.Equal(first) {
		t.Errorf("tool-a started at %v, want %v: an update must not move it", got, first)
	}
	if got := a.GetAt().AsTime(); !got.Equal(updated) {
		t.Errorf("tool-a last updated at %v, want %v", got, updated)
	}
	if got := b.GetStartedAt().AsTime(); !got.Equal(first.Add(5 * time.Second)) {
		t.Errorf("tool-b started at %v, want %v", got, first.Add(5*time.Second))
	}
	if b.GetStatus() != pb.ItemStatus_ITEM_STATUS_FINISHED || !b.GetAt().AsTime().Equal(ended) {
		t.Errorf("the end of the turn left tool-b %v at %v, want finished at %v", b.GetStatus(), b.GetAt().AsTime(), ended)
	}
	for _, it := range page.GetItems() {
		if it.GetStartedAt() == nil || it.GetStartedAt().AsTime().After(it.GetAt().AsTime()) {
			t.Errorf("item %s: started %v after it was last updated %v", it.GetId(), it.GetStartedAt(), it.GetAt())
		}
	}
}

// What an item keeps when it was first seen across a restart of the daemon over its store.
func TestWhenAnItemWasFirstSeenSurvivesARestart(t *testing.T) {
	rig := daemontest.NewRig(t)
	s := openTurn(t, rig)
	first := rig.Clock.Now()
	s.emit(t, 4*time.Second, s.item("tool-a", pb.ItemStatus_ITEM_STATUS_RUNNING, shell("make", "")))
	s.emit(t, time.Second, s.item("tool-a", pb.ItemStatus_ITEM_STATUS_FINISHED, shell("make", "built")))
	rig.Restart(t)
	rig.Settle(t)
	page := s.newest(t)
	got := byID(page, "tool-a")
	if got == nil || !got.GetStartedAt().AsTime().Equal(first) {
		t.Fatalf("after a restart tool-a started at %v, want %v", got.GetStartedAt(), first)
	}
}

// Sizes around the limit, each with the body kind it is given to.
func TestAPageLeavesOutExactlyTheLargeReasoningAndToolOutput(t *testing.T) {
	rig := daemontest.NewRig(t)
	s := openTurn(t, rig)
	r := rand.New(rand.NewPCG(5, 6))
	type made struct {
		id    string
		size  int
		large bool
		kind  string
	}
	var items []made
	pick := func() int {
		switch r.IntN(4) {
		case 0:
			return daemon.ElideOver - r.IntN(3) // at or just under the limit
		case 1:
			return daemon.ElideOver + 1 + r.IntN(3) // just over it
		case 2:
			return r.IntN(50)
		default:
			return daemon.ElideOver*2 + r.IntN(50_000)
		}
	}
	for i := range 40 {
		size := pick()
		text := strings.Repeat("x", size)
		id := "item-" + string(rune('a'+i%26)) + string(rune('a'+i/26))
		kind := [...]string{"reasoning", "tool", "assistant", "prompt"}[i%4]
		var body func(*pb.SessionItem)
		switch kind {
		case "reasoning":
			body = reasoning(text)
		case "tool":
			body = shell(strings.Repeat("c", size), text)
		case "assistant":
			body = assistant(text)
		default:
			body = prompt(text)
		}
		s.emit(t, time.Second, s.item(id, pb.ItemStatus_ITEM_STATUS_FINISHED, body))
		items = append(items, made{id: id, size: size, large: size > daemon.ElideOver, kind: kind})
	}
	s.end(t)
	page := s.newest(t)
	elided := map[uint32]uint64{}
	for _, e := range page.GetElisions() {
		if _, dup := elided[e.GetOrdinal()]; dup {
			t.Fatalf("ordinal %d is elided twice", e.GetOrdinal())
		}
		elided[e.GetOrdinal()] = e.GetBodyBytes()
	}
	left := 0
	for _, m := range items {
		it := byID(page, m.id)
		if it == nil {
			t.Fatalf("%s is not in the page", m.id)
		}
		size, isElided := elided[it.GetOrdinal()]
		if want := m.large && (m.kind == "reasoning" || m.kind == "tool"); isElided != want {
			t.Errorf("%s (%s, %d bytes): elided %v, want %v", m.id, m.kind, m.size, isElided, want)
		}
		text := it.GetReasoning().GetText() + it.GetTool().GetOutput() + it.GetAssistant().GetText() + it.GetPrompt().GetText()
		if isElided {
			left++
			if text != "" || int(size) != m.size {
				t.Errorf("%s: %d bytes of text left in an elided item that was %d bytes (said %d)", m.id, len(text), m.size, size)
			}
			continue
		}
		if len(text) != m.size {
			t.Errorf("%s (%s): %d bytes of %d came through an item that was not elided", m.id, m.kind, len(text), m.size)
		}
		if tool := it.GetTool(); tool != nil && len(tool.GetShell().GetCommand()) != m.size {
			t.Errorf("%s: what the tool was asked changed", m.id)
		}
	}
	if left != len(page.GetElisions()) || left == 0 {
		t.Errorf("%d items elided, page names %d, and the property needs at least one", left, len(page.GetElisions()))
	}
	// What the tool was asked is whole even when its output is not.
	for _, m := range items {
		if m.kind == "tool" && m.large {
			if got := len(byID(page, m.id).GetTool().GetShell().GetCommand()); got != m.size {
				t.Errorf("%s: its command was cut to %d of %d bytes", m.id, got, m.size)
			}
		}
	}
}

// A page leaves things out, and nothing else does: an item that arrives in a stream is whole, and so
// is the same item read again.
func TestAnItemThatArrivesLiveIsWhole(t *testing.T) {
	rig := daemontest.NewRig(t)
	s := openTurn(t, rig)
	w, _, err := rig.Service.WatchAgentFrom(s.lead.ID, 10, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	huge := strings.Repeat("y", daemon.ElideOver*3)
	s.emit(t, time.Second, s.item("think", pb.ItemStatus_ITEM_STATUS_FINISHED, reasoning(huge)))
	s.emit(t, time.Second, s.item("run", pb.ItemStatus_ITEM_STATUS_FINISHED, shell("ls", huge)))
	frames, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, f := range frames {
		if item := f.GetItem(); item != nil {
			seen++
			if got := item.GetReasoning().GetText() + item.GetTool().GetOutput(); got != huge {
				t.Errorf("a live item came with %d of %d bytes", len(got), len(huge))
			}
		}
	}
	if seen != 2 {
		t.Fatalf("saw %d live items, want 2", seen)
	}
}

// GetItem returns the whole item for any ordinal of an agent, including every one a page left out.
func TestGetItemReadsWholeWhatAPageLeftOut(t *testing.T) {
	rig := daemontest.NewRig(t)
	s := openTurn(t, rig)
	huge := strings.Repeat("z", daemon.ElideOver*2+7)
	s.emit(t, time.Second, s.item("a", pb.ItemStatus_ITEM_STATUS_FINISHED, reasoning(huge)))
	s.emit(t, time.Second, s.item("b", pb.ItemStatus_ITEM_STATUS_FINISHED, assistant("short")))
	s.emit(t, time.Second, s.item("c", pb.ItemStatus_ITEM_STATUS_FINISHED, shell("cat big", huge)))
	s.end(t)
	page := s.newest(t)
	if len(page.GetElisions()) != 2 {
		t.Fatalf("the page elided %d items, want 2", len(page.GetElisions()))
	}
	ctx := context.Background()
	for _, it := range page.GetItems() {
		got, err := rig.Service.GetItem(ctx, &pb.GetItemRequest{AgentId: string(s.lead.ID), Ordinal: it.GetOrdinal()})
		if err != nil {
			t.Fatalf("ordinal %d: %v", it.GetOrdinal(), err)
		}
		if got.GetItem().GetOrdinal() != it.GetOrdinal() || got.GetItem().GetId() != it.GetId() {
			t.Errorf("ordinal %d gave item %s at %d", it.GetOrdinal(), got.GetItem().GetId(), got.GetItem().GetOrdinal())
		}
		text := got.GetItem().GetReasoning().GetText() + got.GetItem().GetTool().GetOutput()
		switch it.GetId() {
		case "a", "c":
			if text != huge {
				t.Errorf("item %s came with %d of %d bytes", it.GetId(), len(text), len(huge))
			}
		}
		if got.GetSequence() == 0 {
			t.Errorf("ordinal %d: no sequence", it.GetOrdinal())
		}
		if got.GetItem().GetStartedAt() == nil {
			t.Errorf("ordinal %d: no start time", it.GetOrdinal())
		}
	}
}

// An agent or an ordinal that is not there is not found, and so is the one past the newest item.
func TestGetItemSaysWhatIsNotThere(t *testing.T) {
	rig := daemontest.NewRig(t)
	s := openTurn(t, rig)
	s.emit(t, time.Second, s.item("a", pb.ItemStatus_ITEM_STATUS_FINISHED, assistant("hi")))
	s.end(t)
	newest := s.newest(t).GetItems()
	last := newest[len(newest)-1].GetOrdinal()
	ctx := context.Background()
	for _, tc := range []struct {
		name  string
		agent string
		at    uint32
		want  codes.Code
	}{
		{"an unknown agent", "agent-nobody", 1, codes.NotFound},
		{"the ordinal after the newest", string(s.lead.ID), last + 1, codes.NotFound},
		{"a far ordinal", string(s.lead.ID), last + 1000, codes.NotFound},
		{"the largest ordinal", string(s.lead.ID), ^uint32(0), codes.NotFound},
		{"no ordinal", string(s.lead.ID), 0, codes.InvalidArgument},
		{"no agent", "", 1, codes.InvalidArgument},
		{"the newest item", string(s.lead.ID), last, codes.OK},
		{"the first item", string(s.lead.ID), 1, codes.OK},
	} {
		_, err := rig.Service.GetItem(ctx, &pb.GetItemRequest{AgentId: tc.agent, Ordinal: tc.at})
		if status.Code(err) != tc.want {
			t.Errorf("%s: got %v, want %v", tc.name, err, tc.want)
		}
	}
}

// Reading is the operator's alone.
func TestAnAgentCannotGetAnItem(t *testing.T) {
	rig := daemontest.NewRig(t)
	_, lead := beginRun(t, rig)
	ctx := agentContext(lead.ID, lead.Session, rig.Auth.Token(core.Session{Agent: lead.ID, N: lead.Session}))
	if _, err := rig.AgentService.GetItem(ctx, &pb.GetItemRequest{AgentId: string(lead.ID), Ordinal: 1}); status.Code(err) != codes.PermissionDenied {
		t.Errorf("GetItem by an agent gave %v, want PermissionDenied", err)
	}
}
