package fifo_test

import (
	"fmt"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/scheduletest"
)

func TestFIFOPassesThePolicyContract(t *testing.T) {
	scheduletest.Run(t, scheduletest.Contract{New: fifo.New})
}

// With one slot that every job takes, fifo is the daemon's rule before policies: the one job that
// starts is the oldest waiting, by submission time and then by when the daemon heard of it, and
// nothing starts beside a holder.
func TestWithOneSlotTheOldestJobStartsAlone(t *testing.T) {
	epoch := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for seed := range uint64(2000) {
		r := rand.New(rand.NewPCG(seed, 7))
		s := schedule.State{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 1}}}
		if r.IntN(3) == 0 {
			s.Holdings = []schedule.Holding{{ID: "h", Claims: []schedule.Claim{{Resource: schedule.DefaultPlacement, Amount: 1}}}}
		}
		oldest := -1
		for i := range 1 + r.IntN(8) {
			c := schedule.Candidate{
				ID: schedule.ID(fmt.Sprintf("j%d", i)), Submitted: epoch.Add(time.Duration(r.IntN(3)) * time.Second), Seq: uint64(r.IntN(1000)*10 + i),
				Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: r.IntN(2) == 0}),
			}
			s.Waiting = append(s.Waiting, c)
			if oldest < 0 || c.Submitted.Before(s.Waiting[oldest].Submitted) || (c.Submitted.Equal(s.Waiting[oldest].Submitted) && c.Seq < s.Waiting[oldest].Seq) {
				oldest = i
			}
		}
		var starts []schedule.ID
		for _, d := range schedule.Step(s, schedule.Rules{Policy: fifo.New()}) {
			if st, ok := d.(schedule.Start); ok {
				starts = append(starts, st.ID)
			}
		}
		switch {
		case len(s.Holdings) > 0 && len(starts) != 0:
			t.Fatalf("seed %d: started %v beside a holder", seed, starts)
		case len(s.Holdings) == 0 && (len(starts) != 1 || starts[0] != s.Waiting[oldest].ID):
			t.Fatalf("seed %d: started %v, and the oldest is %s", seed, starts, s.Waiting[oldest].ID)
		}
	}
}
