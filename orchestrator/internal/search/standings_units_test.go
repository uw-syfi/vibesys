package search_test

import (
	"testing"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// A standing says the unit the values are counted in (the series'), apart from the unit of the bound,
// and whether the two agree as ObjectiveBounds says, so a client never reads a bound's unit as the unit
// of a measurement.
func TestAStandingNamesTheMeasuredUnitApartFromTheBoundsAndItsCheck(t *testing.T) {
	bounded := declared("tput", cli.Direction_DIRECTION_MAXIMIZE, "tok/s", ptr(90), nil)
	bounded.UnitCheck = pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH
	series := seriesOf("tput", 40, 90)
	series.Unit = "requests/s"
	state := &pb.SearchState{
		Head:   measuredHead(&cli.MeasureResult{Metric: &cli.Metric{Name: "tput", Direction: cli.Direction_DIRECTION_MAXIMIZE, Unit: "requests/s"}, Value: 90}),
		Bounds: []*pb.ObjectiveBounds{bounded},
		Series: []*pb.ObjectiveSeries{series},
	}
	sameStandings(t, search.Standings(state), []*pb.ObjectiveStanding{
		{Name: "tput", HeadValue: ptr(90), BestValue: ptr(90), Unit: "requests/s", BoundUnit: "tok/s", BoundUnitCheck: pb.BoundUnitCheck_BOUND_UNIT_CHECK_MISMATCH},
	})
}
