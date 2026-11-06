package clientcli_test

import (
	"slices"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// --- the footer, and the end ---

func footerOf(outs []attachview.Output) (string, bool) {
	for _, o := range outs {
		if f, ok := o.(attachview.SetFooter); ok {
			return f.Text, true
		}
	}
	return "", false
}

func TestTheFooterExistsOnlyWithControlsAndFitsTheWidth(t *testing.T) {
	v, agents := conversationWorld(pb.RunStatus_RUN_STATUS_RUNNING)
	lead := agents[0]
	lead.Usage = &pb.Usage{CostUsd: ptr(0.5)}
	v.Load(&pb.RunsSnapshot{Agents: []*pb.Agent{lead}})

	plain, outs := attachview.Begin(attachview.Options{History: 10, Now: epoch, Zone: time.UTC}, v, nil)
	if _, has := footerOf(outs); has {
		t.Error("a view without controls set a footer")
	}
	_, outs = attachview.Update(plain, attachview.Tick{Now: epoch.Add(time.Minute)})
	if _, has := footerOf(outs); has {
		t.Error("a tick set a footer in a view without controls")
	}

	m, outs := attachview.Begin(attachview.Options{Controls: true, History: 10, Now: epoch, Zone: time.UTC}, v, nil)
	footer, has := footerOf(outs)
	if !has {
		t.Fatal("a view with controls has no footer")
	}
	contains(t, "footer", footer, "0m00s", "2 active", "$0.50", "viewing lead", "a agents", "Ctrl-C detaches")
	if strings.Contains(footer, "Esc") {
		t.Errorf("the lead has nowhere to go back to, yet the footer offers it: %q", footer)
	}
	_, outs = attachview.Update(m, attachview.Tick{Now: epoch.Add(65 * time.Second)})
	if footer, _ = footerOf(outs); !strings.Contains(footer, "1m05s") {
		t.Errorf("the footer's clock did not move: %q", footer)
	}
	for width := 1; width <= 130; width++ {
		_, outs = attachview.Update(m, attachview.Resize{Width: width, Height: 24})
		footer, changed := footerOf(outs)
		if changed && utf8.RuneCountInString(footer) > max(width-1, 1) {
			t.Fatalf("at width %d the footer is %d characters: %q", width, utf8.RuneCountInString(footer), footer)
		}
		if width < 60 && !changed {
			t.Fatalf("at width %d the footer was not refitted", width)
		}
	}
}

func TestAViewOfAnEndedRunEndsOnceItsConversationIsShown(t *testing.T) {
	v, _ := conversationWorld(pb.RunStatus_RUN_STATUS_ENDED)
	m, outs := attachview.Begin(attachview.Options{History: 10, Now: epoch, Zone: time.UTC}, v, nil)
	if slices.ContainsFunc(outs, func(o attachview.Output) bool { _, ok := o.(attachview.Exit); return ok }) {
		t.Fatal("the view ended before it had shown anything")
	}
	_, outs = attachview.Update(m, attachview.AgentHistory{Gen: subscribeOf(outs).Gen, Items: []*pb.Item{assistantItem("lead-run-1", 1, "t", "all of it")}})
	exit, ok := outs[len(outs)-1].(attachview.Exit)
	if !ok || exit.Cause != attachview.ExitEnded {
		t.Fatalf("the replay of an ended run did not end the view: %v", outs)
	}
	if slices.ContainsFunc(outs, func(o attachview.Output) bool { _, ok := o.(attachview.Sweep); return ok }) {
		t.Error("an ended run was read a second time")
	}
}

// What the run's stream says has happened may not have reached the agent's stream yet, so a run that
// ends under a view is read once more, and the view ends when that has been shown.
func TestARunThatEndsUnderTheViewIsReadOnceMoreBeforeTheViewEnds(t *testing.T) {
	v, _ := conversationWorld(pb.RunStatus_RUN_STATUS_RUNNING)
	m, outs := attachview.Begin(attachview.Options{History: 10, Now: epoch, Zone: time.UTC}, v, nil)
	gen := subscribeOf(outs).Gen
	m, _ = attachview.Update(m, attachview.AgentHistory{Gen: gen, Items: []*pb.Item{assistantItem("lead-run-1", 1, "t", "first")}})
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	ended.EndedAt = at(9)
	m, outs = attachview.Update(m, attachview.RunFrame{Change: &pb.RunChange{Sequence: 2, RunId: "run-1", At: at(9), Body: &pb.RunChange_Run{Run: ended}}})
	var sweep attachview.Sweep
	for _, o := range outs {
		switch o := o.(type) {
		case attachview.Sweep:
			sweep = o
		case attachview.Exit:
			t.Fatal("the view ended before the agent's last items were read")
		}
	}
	if !sweep.Final || sweep.Gen != gen || sweep.Agent != "lead-run-1" {
		t.Fatalf("sweep = %+v", sweep)
	}
	m, outs = attachview.Update(m, attachview.AgentSweep{Gen: gen, Final: true, Items: []*pb.Item{
		assistantItem("lead-run-1", 1, "t", "first"), assistantItem("lead-run-1", 2, "t", "last words")}})
	if got := strings.Join(printed(outs), "\n"); !strings.Contains(got, "* last words") || strings.Contains(got, "* first") {
		t.Errorf("the final read printed %q, want only what was new", got)
	}
	if !m.Done() {
		t.Error("the view did not end after the final read")
	}
}
