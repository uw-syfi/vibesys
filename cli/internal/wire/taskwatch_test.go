package wire_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// reader is a client of WatchTask: it holds a Reset and the facts after it aside and replaces its
// view only at CaughtUp, and it keeps its resume token to the last CaughtUp.
type reader struct {
	view      map[string]string
	pending   map[string]string
	resetting bool
	token     uint64
	last      uint64
}

func (r *reader) apply(m *cliv1.WatchTaskResponse) error {
	if m.GetSequence() != r.last+1 {
		return fmt.Errorf("sequence %d after %d: a gap", m.GetSequence(), r.last)
	}
	r.last = m.GetSequence()
	switch b := m.GetBody().(type) {
	case *cliv1.WatchTaskResponse_Reset_:
		r.resetting, r.pending = true, map[string]string{}
	case *cliv1.WatchTaskResponse_Facts:
		into := r.view
		if r.resetting {
			into = r.pending
		}
		if len(b.Facts.GetFacts()) == 0 || len(b.Facts.GetFacts()) > core.MaxFactsPerMessage {
			return fmt.Errorf("a message of %d facts", len(b.Facts.GetFacts()))
		}
		for _, f := range b.Facts.GetFacts() {
			into[factKey(f)] = fmt.Sprint(f)
		}
	case *cliv1.WatchTaskResponse_CaughtUp:
		if !r.resetting {
			return fmt.Errorf("CaughtUp with no Reset")
		}
		r.view, r.pending, r.resetting, r.token = r.pending, nil, false, r.last
	}
	return nil
}

func factKey(f *cliv1.TaskFact) string {
	switch v := f.GetFact().(type) {
	case *cliv1.TaskFact_Job:
		return "job/" + v.Job.GetId()
	case *cliv1.TaskFact_Result:
		return "result/" + v.Result.GetJobId()
	case *cliv1.TaskFact_Judgment:
		return "judgment/" + v.Judgment.GetCommit()
	case *cliv1.TaskFact_Promotion:
		return fmt.Sprint("promotion/", v.Promotion.GetSeq())
	case *cliv1.TaskFact_Plan:
		return fmt.Sprint("plan/", v.Plan.GetNumber())
	case *cliv1.TaskFact_Head:
		return "head"
	case *cliv1.TaskFact_Health:
		return "health"
	case *cliv1.TaskFact_Trouble:
		return "trouble/" + v.Trouble.GetJobId()
	}
	return "?"
}

func viewOf(v core.TaskView) map[string]string {
	out := map[string]string{}
	for _, f := range v.Facts() {
		m, _ := wire.TaskFact(f)
		out[factKey(m)] = fmt.Sprint(m)
	}
	return out
}

func manyJobs(r *rand.Rand, max int) core.TaskView {
	c := &core.TaskContent{Head: core.NoCurrentResult{Reason: core.NoHead}}
	for n := 1; n <= r.IntN(max); n++ {
		c.Jobs = append(c.Jobs, core.Job{ID: core.JobID(fmt.Sprintf("j%d", n)), State: []core.JobState{core.JobQueued, core.JobRunning, core.JobSucceeded}[r.IntN(3)]})
	}
	return core.TaskView{Content: c}
}

func (r *reader) assertSome(t *testing.T, seed uint64, looks []map[string]string) {
	t.Helper()
	for _, l := range looks {
		if len(l) == len(r.view) && reflect.DeepEqual(l, r.view) {
			return
		}
	}
	// A view built by changes applies facts one message at a time, but each message of a change is
	// whole, so the view is still a look.
	t.Fatalf("seed %d: the view of %d facts is none of the looks", seed, len(r.view))
}

func TestJudgedCommitsAreCheckedInWire(t *testing.T) {
	for _, tc := range []struct {
		asked uint32
		want  int
		bad   bool
	}{{0, 20, false}, {20, 20, false}, {100, 100, false}, {101, 0, true}} {
		got, err := wire.JudgedCommits(&cliv1.WatchTaskRequest{Context: &cliv1.CallContext{}, JudgedCommits: tc.asked})
		if (err != nil) != tc.bad || got != tc.want || (tc.bad && !wire.IsInvalidInput(err)) {
			t.Errorf("JudgedCommits(%d) = %d, %v", tc.asked, got, err)
		}
	}
}

func TestAResetOfManyFactsIsSeveralMessagesOfAtMostTheLimit(t *testing.T) {
	v := manyJobs(rand.New(rand.NewPCG(1, 1)), 1)
	c := v.Content
	for n := 1; n <= core.MaxFactsPerMessage*2+7; n++ {
		c.Jobs = append(c.Jobs, core.Job{ID: core.JobID(fmt.Sprintf("k%d", n)), State: core.JobRunning})
	}
	s, _ := core.PlanSend(nil, v)
	msgs := wire.TaskMessages(s)
	if _, ok := msgs[0].GetBody().(*cliv1.WatchTaskResponse_Reset_); !ok {
		t.Fatal("the first message of a reset is not a Reset")
	}
	if _, ok := msgs[len(msgs)-1].GetBody().(*cliv1.WatchTaskResponse_CaughtUp); !ok {
		t.Fatal("the last message of a reset is not CaughtUp")
	}
	total := 0
	for _, m := range msgs[1 : len(msgs)-1] {
		n := len(m.GetFacts().GetFacts())
		if n == 0 || n > core.MaxFactsPerMessage {
			t.Fatalf("a message of %d facts", n)
		}
		total += n
	}
	if total != len(v.Facts()) {
		t.Fatalf("the reset carries %d facts of %d", total, len(v.Facts()))
	}
}
