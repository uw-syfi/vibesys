package clientcli_test

import (
	"fmt"
	"math/rand/v2"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

var (
	turnEndLine = regexp.MustCompile(`lead turn (\d+) completed(?:, (\d+) tool calls?)?`)
)

// cutAgent is a lead whose store holds turns of random numbers of items, tool calls among them.
type cutAgent struct {
	items []*pb.Item
	turns []*pb.Turn
	tools map[uint32]int // the real number of tool calls of each turn, by its number.
	first map[uint32]int // the ordinal of the first item of each turn, by its number.
}

func newCutAgent(r *rand.Rand, turns int) *cutAgent {
	a := &cutAgent{tools: map[uint32]int{}, first: map[uint32]int{}}
	for n := uint32(1); n <= uint32(turns); n++ {
		a.addTurn(r, n, 1+r.IntN(9))
	}
	return a
}

func (a *cutAgent) addTurn(r *rand.Rand, n uint32, items int) {
	id := fmt.Sprintf("t%d", n)
	a.turns = append(a.turns, &pb.Turn{Id: id, AgentId: "lead-run-1", Ordinal: n, Trigger: pb.TurnTrigger_TURN_TRIGGER_REMINDER, Status: pb.TurnStatus_TURN_STATUS_COMPLETED,
		StartedAt: at(int(n) * 100), EndedAt: at(int(n)*100 + 50)})
	for range items {
		ord := uint32(len(a.items) + 1)
		if a.first[n] == 0 {
			a.first[n] = int(ord)
		}
		if r.IntN(2) == 0 {
			a.items = append(a.items, toolItem("lead-run-1", ord, id, shell(fmt.Sprintf("cmd %d", ord))))
			a.tools[n]++
		} else {
			a.items = append(a.items, assistantItem("lead-run-1", ord, id, fmt.Sprintf("said %d", ord)))
		}
	}
}

// checkCounts asserts what a printed turn line may say: a tool count, when there is one, is the
// turn's real count, and a turn all of whose items the view was given says its count.
func (a *cutAgent) checkCounts(t *testing.T, label string, lines []string, given func(n uint32) bool) {
	t.Helper()
	text := strings.Join(lines, "\n")
	said := map[uint32]bool{}
	for _, m := range turnEndLine.FindAllStringSubmatch(text, -1) {
		n64, _ := strconv.ParseUint(m[1], 10, 32)
		n := uint32(n64)
		said[n] = true
		if m[2] == "" {
			continue
		}
		if got, _ := strconv.Atoi(m[2]); got != a.tools[n] {
			t.Fatalf("%s: turn %d says %d tool calls, it made %d\n%s", label, n, got, a.tools[n], text)
		}
	}
	for _, turn := range a.turns {
		n := turn.GetOrdinal()
		if given(n) && said[n] && !strings.Contains(text, fmt.Sprintf("lead turn %d completed, %d tool call", n, a.tools[n])) {
			t.Fatalf("%s: turn %d was given whole but does not say its count of %d\n%s", label, n, a.tools[n], text)
		}
	}
}

// Over any turns and any history limit, a count of tool calls on a turn line is the turn's real count:
// a turn the limit cut says none, and a turn that was whole says its own.
func TestATurnCutByTheHistoryLimitSaysNoToolCountAndOnlyTrueOnes(t *testing.T) {
	for seed := range uint64(400) {
		r := rand.New(rand.NewPCG(seed, 33))
		a := newCutAgent(r, 1+r.IntN(6))
		limit := 1 + r.IntN(len(a.items)+3)
		items := a.items
		if len(items) > limit {
			items = items[len(items)-limit:]
		}
		v := attachview.NewView("run-1")
		run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
		v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)}, Turns: a.turns, Sequence: 1})
		m, outs := attachview.Begin(attachview.Options{History: limit, Now: epoch, Zone: time.UTC}, v, nil)
		lines := printed(outs)
		_, outs = attachview.Update(m, attachview.AgentHistory{Gen: subscribeOf(outs).Gen, Items: items})
		lines = append(lines, printed(outs)...)

		// The turn of the first item shown may begin there or earlier, which the view cannot tell, so it
		// is not counted on to say its count; every later turn is whole.
		whole := func(n uint32) bool { return a.first[n] > int(items[0].GetOrdinal()) }
		a.checkCounts(t, fmt.Sprintf("seed %d limit %d", seed, limit), lines, whole)
	}
}

// A turn cut by the history limit says what the transcript could not: no count; the start it says is
// the turn record's own.
func TestACutTurnIsShownWithoutACount(t *testing.T) {
	var items []*pb.Item
	for i := uint32(1); i <= 10; i++ {
		items = append(items, toolItem("lead-run-1", i, "t1", shell(fmt.Sprintf("cmd %d", i))))
	}
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	turn := &pb.Turn{Id: "t1", AgentId: "lead-run-1", Ordinal: 1, Trigger: pb.TurnTrigger_TURN_TRIGGER_REMINDER, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(1), EndedAt: at(30)}
	v := attachview.NewView("run-1")
	v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)}, Turns: []*pb.Turn{turn}, Sequence: 1})
	got := strings.Join(shown(t, v, "", 100, items[4:]...), "\n")
	if !strings.Contains(got, "lead turn 1 completed\n") && !strings.HasSuffix(got, "lead turn 1 completed") {
		t.Errorf("the cut turn should say it completed with no count:\n%s", got)
	}
	if strings.Contains(got, "tool call,") || strings.Contains(got, "tool calls") {
		t.Errorf("a count was said of a turn the limit cut:\n%s", got)
	}
}

// When a catch-up read holds fewer items than the view missed, the turns it missed items of say no
// count either.
func TestAGapBetweenReadsLeavesNoCountOnTheTurnsItMissed(t *testing.T) {
	for seed := range uint64(300) {
		r := rand.New(rand.NewPCG(seed, 34))
		a := newCutAgent(r, 2)
		known := len(a.items)
		// The lead's next turn makes more items than a catch-up page holds.
		a.addTurn(r, 3, 6+r.IntN(10))
		page := 1 + r.IntN(5)

		v := attachview.NewView("run-1")
		run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
		v.Load(&pb.RunsSnapshot{Runs: []*pb.Run{run}, Agents: []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)}, Turns: a.turns[:2], Sequence: 1})
		m, outs := attachview.Begin(attachview.Options{History: 200, Now: epoch, Zone: time.UTC}, v, nil)
		gen := subscribeOf(outs).Gen
		lines := printed(outs)
		m, outs = attachview.Update(m, attachview.AgentHistory{Gen: gen, Items: a.items[:known]})
		lines = append(lines, printed(outs)...)

		third := proto.Clone(a.turns[2]).(*pb.Turn)
		third.Status = pb.TurnStatus_TURN_STATUS_RUNNING
		third.EndedAt = nil
		m, outs = attachview.Update(m, attachview.RunFrame{Change: &pb.RunChange{Sequence: 2, RunId: "run-1", At: at(300), Body: &pb.RunChange_Turn{Turn: third}}})
		lines = append(lines, printed(outs)...)
		m, outs = attachview.Update(m, attachview.RunFrame{Change: &pb.RunChange{Sequence: 3, RunId: "run-1", At: at(350), Body: &pb.RunChange_Turn{Turn: a.turns[2]}}})
		lines = append(lines, printed(outs)...)
		newest := a.items[len(a.items)-page:]
		_, outs = attachview.Update(m, attachview.AgentSweep{Gen: gen, Items: newest})
		lines = append(lines, printed(outs)...)

		whole := func(n uint32) bool { return n < 3 || len(a.items)-page+1 <= a.first[3] }
		a.checkCounts(t, fmt.Sprintf("seed %d page %d", seed, page), lines, whole)
	}
}
