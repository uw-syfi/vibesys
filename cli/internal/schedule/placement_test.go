package schedule_test

import (
	"fmt"
	"math/rand/v2"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// For any job: a docker job on a placement claims a slot of it first and then what its resources
// claim; any other job claims a slot of the host. A measure claims every one of them whole, and
// another job only what a resource itself claims whole.
func TestAJobClaimsItsPlacementAndItsResourcesAndAMeasureClaimsThemWhole(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	envs := []core.EnvironmentKind{core.EnvironmentDocker, core.EnvironmentHost}
	settings := []core.Settings{core.CheckSettings{}, core.MeasureSettings{}, core.CommandSettings{Argv: []string{"true"}}}
	for range 500 {
		d := schedule.PlacementDemand{Placements: map[string][]schedule.Claim{}, Host: "host"}
		names := []string{"", "p0", "p1", "p2"}
		for _, name := range names[1:] {
			for i := range r.IntN(3) {
				d.Placements[name] = append(d.Placements[name], schedule.Claim{
					Resource: schedule.ResourceID(fmt.Sprintf("r%d", i)), Amount: r.IntN(3), Exclusive: r.IntN(4) == 0,
				})
			}
		}
		job := core.Job{Spec: core.JobSpec{
			Environment: envs[r.IntN(len(envs))], Placement: names[r.IntN(len(names))], Settings: settings[r.IntN(len(settings))],
		}}
		measure := job.Spec.Settings.Kind() == core.JobMeasure
		got := d.Claims(job)
		if job.Spec.Environment != core.EnvironmentDocker || job.Spec.Placement == "" {
			if len(got) != 1 || got[0] != (schedule.Claim{Resource: "host", Amount: 1, Exclusive: measure}) {
				t.Fatalf("%+v claims %+v, want a slot of the host", job.Spec, got)
			}
			continue
		}
		own := d.Placements[job.Spec.Placement]
		if len(got) != 1+len(own) || got[0] != (schedule.Claim{Resource: schedule.PlacementOf(job.Spec.Placement), Amount: 1, Exclusive: measure}) {
			t.Fatalf("%+v claims %+v, want a slot of its placement and %+v", job.Spec, got, own)
		}
		for i, c := range own {
			want := schedule.Claim{Resource: c.Resource, Amount: max(c.Amount, 1), Exclusive: c.Exclusive || measure}
			if got[1+i] != want {
				t.Fatalf("%+v claims %+v of %s, want %+v", job.Spec, got[1+i], c.Resource, want)
			}
		}
	}
}
