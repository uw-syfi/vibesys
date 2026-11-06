package main

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
)

type gainCheckJSON struct {
	Share    float64 `json:"share"`
	Answer   string  `json:"answer"`
	Counted  uint32  `json:"counted"`
	Blockers []struct {
		Number uint32 `json:"number"`
	} `json:"blockers"`
}

// AllUnder is the answer's yes. The answer is always present in the JSON, as a name.
func (g gainCheckJSON) AllUnder() bool { return g.Answer == "PLAN_GAIN_ANSWER_ALL_UNDER" }

func (c *cli) gainCheck(under string) gainCheckJSON {
	c.t.Helper()
	r := c.planOK("check-gain", "--under", under, "--json")
	var out gainCheckJSON
	if err := json.Unmarshal([]byte(r.stdout), &out); err != nil {
		c.t.Fatalf("check-gain --json: %q is not JSON: %v", r.stdout, err)
	}
	return out
}

func TestAPlanStatesItsGainBoundAndTheListShowsIt(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	if r := c.planOK("create", "--title", "fuse", "--gain-bound", "5%"); r.stdout != "1\topen\tfuse\tgain <= 5% of headline\n" {
		t.Fatalf("create = %q", r.stdout)
	}
	c.planOK("create", "--title", "bare")
	if got := c.planOK("list").stdout; got != "1\topen\tfuse\tgain <= 5% of headline\n2\topen\tbare\n" {
		t.Fatalf("list = %q", got)
	}
	// An edit that sets only the bound is an edit, and replaces the bound.
	if r := c.planOK("edit", "2", "--gain-bound", "0.25", "--gain-bound-of", "headline"); r.stdout != "2\topen\tbare\tgain <= 25% of headline\n" {
		t.Fatalf("edit = %q", r.stdout)
	}
	// A bound survives other edits and a closing.
	c.planOK("edit", "1", "--title", "fuse loops")
	c.planOK("close", "1", "--status", "parked", "--comment", "later")
	if got := c.planOK("view", "1").stdout; !strings.HasPrefix(got, "1\tparked\tfuse loops\tgain <= 5% of headline\n") {
		t.Fatalf("view = %q", got)
	}
}

func TestACheckSaysWhetherEveryOpenOrParkedPlanIsUnderTheShare(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	if got := c.gainCheck("10%"); !got.AllUnder() || got.Counted != 0 || len(got.Blockers) != 0 {
		t.Fatalf("no plans: %+v", got)
	}
	c.planOK("create", "--title", "small", "--gain-bound", "0.04")
	c.planOK("create", "--title", "big", "--gain-bound", "30%")
	c.planOK("create", "--title", "unstated")
	c.planOK("create", "--title", "done", "--gain-bound", "90%")
	c.planOK("close", "4", "--status", "won")
	got := c.gainCheck("10%")
	if got.AllUnder() || got.Counted != 3 || len(got.Blockers) != 2 || got.Blockers[0].Number != 2 || got.Blockers[1].Number != 3 {
		t.Fatalf("with a big and an unstated plan: %+v", got)
	}
	text := c.planOK("check-gain", "--under", "0.1").stdout
	if !strings.HasPrefix(text, "no: 2 of 3 open or parked plans do not state a gain bound below 10%\n") ||
		!strings.Contains(text, "2\topen\tbig\tgain <= 30% of headline\n") || !strings.Contains(text, "3\topen\tunstated\tno gain bound\n") {
		t.Fatalf("text = %q", text)
	}
	c.planOK("close", "2", "--status", "rejected")
	c.planOK("edit", "3", "--gain-bound", "0.099")
	if got := c.gainCheck("10%"); !got.AllUnder() || got.Counted != 2 || len(got.Blockers) != 0 {
		t.Fatalf("after closing the big one and bounding the other: %+v", got)
	}
	if text := c.planOK("check-gain", "--under", "10%").stdout; text != "yes: all 2 open or parked plans state a gain bound below 10%\n" {
		t.Fatalf("text = %q", text)
	}
	// A bound equal to the share is not below it.
	c.planOK("edit", "3", "--gain-bound", "10%")
	if got := c.gainCheck("10%"); got.AllUnder() {
		t.Fatalf("a bound of exactly the share is under it: %+v", got)
	}
}

// Whatever bounds the plans state, the answer follows them: each plan's own bound is the line
// between yes and no for a check at that share.
func TestTheCheckAgreesWithTheBoundsStated(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	r := rand.New(rand.NewPCG(3, 4))
	var shares []float64
	for i := range 8 {
		share := float64(r.IntN(50)) / 100
		shares = append(shares, share)
		c.planOK("create", "--title", fmt.Sprintf("p%d", i), "--gain-bound", fmt.Sprint(share))
	}
	for range 30 {
		under := float64(r.IntN(60)) / 100
		var want uint32
		for _, s := range shares {
			if !(s < under) {
				want++
			}
		}
		if got := c.gainCheck(fmt.Sprint(under)); got.AllUnder() != (want == 0) || uint32(len(got.Blockers)) != want || got.Counted != 8 {
			t.Fatalf("under %v with bounds %v: %+v, want %d blockers", under, shares, got, want)
		}
	}
}

func TestAMistakenGainBoundIsAUsageErrorAndStartsNoDaemon(t *testing.T) {
	c := newCLI(t)
	for _, args := range [][]string{
		{"create", "--title", "t", "--gain-bound", "-1"},
		{"create", "--title", "t", "--gain-bound", "lots"},
		{"create", "--title", "t", "--gain-bound", "nan"},
		{"create", "--title", "t", "--gain-bound", "inf"},
		{"create", "--title", "t", "--gain-bound", "5%%"},
		{"create", "--title", "t", "--gain-bound", "5%", "--gain-bound-of", "base"},
		{"create", "--title", "t", "--gain-bound-of", "headline"},
		{"check-gain"},
		{"check-gain", "--under", "x"},
		{"check-gain", "--under", "-5%"},
		{"check-gain", "--under", "10%", "extra"},
	} {
		r := c.plan(args[0], args[1:]...)
		if r.code != exitInvalidInput || r.stdout != "" || r.stderr == "" {
			t.Errorf("plan %v = %+v, want invalid input", args, r)
		}
	}
}
