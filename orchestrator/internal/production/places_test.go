package production

import (
	"context"
	"errors"
	"testing"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// answeringTool is a vibesys-cli whose `daemon ensure` names a socket.
type answeringTool struct{ ran []string }

func (a *answeringTool) Run(_ context.Context, program string, _, _ []string) ([]byte, []byte, error) {
	a.ran = append(a.ran, program)
	out, err := protojson.Marshal(&cli.DaemonEnsureResponse{
		Socket: "/run/test/daemon.sock", RuntimeDirectory: "/run/test",
		Versions: &cli.BuildVersions{BinaryVersion: "test", ProtocolVersion: 3, MinProtocolVersion: 1},
	})
	return out, nil, err
}

// placementDaemon answers the one report it holds.
type placementDaemon struct {
	closed    int
	refreshed bool
}

func (d *placementDaemon) ListPlacements(_ context.Context, r *cli.ListPlacementsRequest) (*cli.ListPlacementsResponse, error) {
	d.refreshed = r.GetRefresh()
	return &cli.ListPlacementsResponse{Placements: []*cli.PlacementView{{
		Name: "local", Kind: cli.PlacementKind_PLACEMENT_KIND_LOCAL,
		Reach:  &cli.PlacementReach{State: cli.ReachState_REACH_STATE_REACHABLE},
		Offers: &cli.PlacementView_Known{Known: &cli.PlacementOffers{Source: cli.OfferSource_OFFER_SOURCE_PROBED}},
	}}}, nil
}

func (d *placementDaemon) Close() error { d.closed++; return nil }

func TestCLIPlacesAsksTheToolWhereItsDaemonIsThenTheDaemon(t *testing.T) {
	if err := protovalidate.Validate(&cli.DaemonEnsureResponse{Socket: "/run/test/daemon.sock", RuntimeDirectory: "/run/test", Versions: &cli.BuildVersions{BinaryVersion: "test", ProtocolVersion: 3, MinProtocolVersion: 1}}); err != nil {
		t.Fatalf("the fixture answer is not what the tool says: %v", err)
	}
	tool, daemon := &answeringTool{}, &placementDaemon{}
	var dialed clidaemon.Socket
	source := CLIPlaces{
		JobTool: func(context.Context) (core.JobToolEndpoint, error) {
			return core.JobToolEndpoint{Executable: "/opt/vibesys/bin/vibesys-cli"}, nil
		},
		Runner: tool,
		Connect: func(_ context.Context, s clidaemon.Socket) (clidaemon.PlacementConnection, error) {
			dialed = s
			return daemon, nil
		},
	}
	places, err := source.Places(bounded(t), true)
	if err != nil || len(places) != 1 || places[0].Name != "local" {
		t.Fatalf("places %v, %v", places, err)
	}
	if len(tool.ran) != 1 || tool.ran[0] != "/opt/vibesys/bin/vibesys-cli" {
		t.Errorf("ran %v, want the recorded tool once", tool.ran)
	}
	if dialed.Path() != "/run/test/daemon.sock" || daemon.closed != 1 || !daemon.refreshed {
		t.Errorf("dialed %q, closed %d, refresh %v", dialed.Path(), daemon.closed, daemon.refreshed)
	}
}

func TestCLIPlacesFailsWhenTheHostHasNoToolOrTheToolIsNotThere(t *testing.T) {
	if _, err := (CLIPlaces{}).Places(context.Background(), false); err == nil {
		t.Error("a host with no job tool reported places")
	}
	gone := errors.New("no vibesys-cli")
	source := CLIPlaces{JobTool: func(context.Context) (core.JobToolEndpoint, error) { return core.JobToolEndpoint{}, gone }}
	if _, err := source.Places(context.Background(), false); !errors.Is(err, gone) {
		t.Errorf("got %v, want the endpoint's error", err)
	}
	if placesOf(nil) != nil {
		t.Error("a host with no job tool has a place source")
	}
}
