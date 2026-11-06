package schedule_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
)

var priorityEpoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

// Over random queues of jobs of every priority, submitted over five hours, under arrival order: a
// job that has waited twice core.PriorityAging is considered before every job submitted after it,
// whatever their priorities, so no job is starved by later ones of a higher priority; and a job of a
// higher priority than another, once aged, is never considered after it.
func TestAJobThatWaitedTwiceTheAgingIsConsideredBeforeEveryLaterOne(t *testing.T) {
	for seed := range uint64(2000) {
		r := rand.New(rand.NewPCG(seed, 11))
		now := priorityEpoch.Add(5 * time.Hour)
		s := schedule.State{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 1 + r.IntN(3)}}, Now: now}
		for i := range 2 + r.IntN(12) {
			s.Waiting = append(s.Waiting, schedule.Candidate{
				ID: schedule.ID(fmt.Sprintf("j%d", i)), Seq: uint64(i),
				Submitted: priorityEpoch.Add(time.Duration(r.IntN(5*60)) * time.Minute),
				Priority:  []core.Priority{"", core.PriorityLow, core.PriorityNormal, core.PriorityHigh}[r.IntN(4)],
				Options:   schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: r.IntN(3) == 0}),
			})
		}
		var order []schedule.Candidate
		for _, d := range schedule.Step(s, schedule.Rules{Policy: fifo.New()}) {
			i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == d.Target() })
			order = append(order, s.Waiting[i])
		}
		for i, a := range order {
			for _, b := range order[i+1:] {
				if now.Sub(b.Submitted) >= 2*core.PriorityAging && b.Submitted.Before(a.Submitted) {
					t.Fatalf("seed %d: %s, submitted at %v, waited %v and was considered after %s, submitted later at %v",
						seed, b.ID, b.Submitted, now.Sub(b.Submitted), a.ID, a.Submitted)
				}
				if schedule.Rank(b, now).Level() > schedule.Rank(a, now).Level() {
					t.Fatalf("seed %d: %s counts as %s and was considered after %s, which counts as %s", seed, b.ID, schedule.Rank(b, now), a.ID, schedule.Rank(a, now))
				}
			}
		}
	}
}

// One slot, a low job that waits, and a high job arriving every minute while the job before ends:
// the low job still starts within twice core.PriorityAging, and not before it counts as high.
func TestALowJobStartsWithinTwiceTheAgingUnderAStreamOfHighJobs(t *testing.T) {
	s := schedule.State{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 1}}}
	claim := schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1})
	s.Waiting = append(s.Waiting, schedule.Candidate{ID: "low", Submitted: priorityEpoch, Priority: core.PriorityLow, Options: claim})
	bound := int(2*core.PriorityAging/time.Minute) + 1
	for minute := 1; minute <= bound+1; minute++ {
		s.Now = priorityEpoch.Add(time.Duration(minute) * time.Minute)
		s.Holdings = nil
		s.Waiting = append(s.Waiting, schedule.Candidate{
			ID: schedule.ID(fmt.Sprintf("high%d", minute)), Seq: uint64(minute), Submitted: s.Now, Priority: core.PriorityHigh, Options: claim,
		})
		for _, d := range schedule.Step(s, schedule.Rules{Policy: fifo.New()}) {
			st, ok := d.(schedule.Start)
			if !ok {
				continue
			}
			if st.ID == "low" {
				if waited := s.Now.Sub(priorityEpoch); waited < 2*core.PriorityAging {
					t.Fatalf("the low job started after %v, before it counted as high, ahead of a waiting high job", waited)
				}
				return
			}
			s.Holdings = []schedule.Holding{{ID: st.ID, Claims: st.Option.Claims}}
			s.Waiting = slices.DeleteFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == st.ID })
		}
	}
	t.Fatalf("the low job did not start within %d minutes", bound)
}
