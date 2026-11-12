package clidaemon_test

import (
	"context"
	"errors"
	"math/rand"
	"strings"
	"testing"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
)

// fakePlacements is a daemon's placement report, and counts the refreshes it was asked for.
type fakePlacements struct {
	resp      *cli.ListPlacementsResponse
	err       error
	refreshes int
	closed    int
}

func (f *fakePlacements) ListPlacements(_ context.Context, r *cli.ListPlacementsRequest) (*cli.ListPlacementsResponse, error) {
	if r.GetRefresh() {
		f.refreshes++
	}
	return f.resp, f.err
}

func (f *fakePlacements) Close() error { f.closed++; return nil }

var probedAt = timestamppb.New(time.Date(2026, 11, 7, 12, 0, 0, 0, time.UTC))

func offer(vendor cli.AcceleratorVendor, model cli.AcceleratorModel, count uint32, name string) *cli.AcceleratorOffer {
	return &cli.AcceleratorOffer{Vendor: vendor, Model: model, Count: count, DeviceName: name}
}

func reach(state cli.ReachState, reason string) *cli.PlacementReach {
	return &cli.PlacementReach{State: state, Reason: reason}
}

func known(source cli.OfferSource, at *timestamppb.Timestamp, offers ...*cli.AcceleratorOffer) *cli.PlacementView_Known {
	return &cli.PlacementView_Known{Known: &cli.PlacementOffers{Offers: offers, Source: source, ProbedAt: at}}
}

func TestEachPlacementStateMapsToTheEvalPlaceThatSaysWhatFits(t *testing.T) {
	h100 := offer(cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cli.AcceleratorModel_ACCELERATOR_MODEL_H100, 8, "NVIDIA H100")
	mi300 := offer(cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300X, 4, "AMD MI300X")
	unknownGPU := offer(cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER, 1, "Tesla T4")
	for name, tc := range map[string]struct {
		view      *cli.PlacementView
		kind      pb.EvalPlaceKind
		offers    []*cli.AcceleratorOffer
		known     *pb.OffersKnown
		unreadble *pb.OffersUnreadable
	}{
		"probed and reachable": {
			view: &cli.PlacementView{Name: "host-a", Kind: cli.PlacementKind_PLACEMENT_KIND_SSH, Reach: reach(cli.ReachState_REACH_STATE_REACHABLE, ""),
				Offers: known(cli.OfferSource_OFFER_SOURCE_PROBED, probedAt, h100, unknownGPU)},
			kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH, offers: []*cli.AcceleratorOffer{h100, unknownGPU},
			known: &pb.OffersKnown{Source: pb.OffersSource_OFFERS_SOURCE_PROBED, ProbedAt: probedAt},
		},
		"a placement with no accelerator is known and offers none": {
			view: &cli.PlacementView{Name: "local", Kind: cli.PlacementKind_PLACEMENT_KIND_LOCAL, Reach: reach(cli.ReachState_REACH_STATE_REACHABLE, ""),
				Offers: known(cli.OfferSource_OFFER_SOURCE_PROBED, probedAt)},
			kind:  pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL,
			known: &pb.OffersKnown{Source: pb.OffersSource_OFFERS_SOURCE_PROBED, ProbedAt: probedAt},
		},
		"not probed with declared offers is known, from the declaration": {
			view: &cli.PlacementView{Name: "cluster-a", Kind: cli.PlacementKind_PLACEMENT_KIND_SLURM, Reach: reach(cli.ReachState_REACH_STATE_NOT_PROBED, ""),
				Offers: known(cli.OfferSource_OFFER_SOURCE_DECLARED, nil, mi300)},
			kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_SLURM, offers: []*cli.AcceleratorOffer{mi300},
			known: &pb.OffersKnown{Source: pb.OffersSource_OFFERS_SOURCE_DECLARED},
		},
		"unreachable is unreadable with the reason, whatever offers it still holds": {
			view: &cli.PlacementView{Name: "host-a", Kind: cli.PlacementKind_PLACEMENT_KIND_SSH, Reach: reach(cli.ReachState_REACH_STATE_UNREACHABLE, "connection refused"),
				Offers: known(cli.OfferSource_OFFER_SOURCE_PROBED, probedAt, h100)},
			kind:      pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH,
			unreadble: &pb.OffersUnreadable{Reason: pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_UNREACHABLE, Detail: "connection refused"},
		},
		"unknown offers carry the daemon's reason": {
			view: &cli.PlacementView{Name: "cluster-b", Kind: cli.PlacementKind_PLACEMENT_KIND_APPTAINER, Reach: reach(cli.ReachState_REACH_STATE_NOT_PROBED, ""),
				Offers: &cli.PlacementView_Unknown{Unknown: &cli.OffersUnknown{Reason: cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_NOT_PROBEABLE, Detail: "declare offers in cli.toml"}}},
			kind:      pb.EvalPlaceKind_EVAL_PLACE_KIND_APPTAINER,
			unreadble: &pb.OffersUnreadable{Reason: pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_NOT_PROBEABLE, Detail: "declare offers in cli.toml"},
		},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := clidaemon.EvalPlace(tc.view)
			if err != nil {
				t.Fatal(err)
			}
			if got.Name != tc.view.Name || got.Kind != tc.kind {
				t.Errorf("identity %q %v, want %q %v", got.Name, got.Kind, tc.view.Name, tc.kind)
			}
			if len(got.Offers) != len(tc.offers) {
				t.Fatalf("offers %v, want %v", got.Offers, tc.offers)
			}
			for i, want := range tc.offers {
				o := got.Offers[i]
				if o.Vendor != want.Vendor || o.Model != want.Model || o.Count != want.Count || o.DeviceName != want.DeviceName {
					t.Errorf("offer %d: %v, want %v", i, o, want)
				}
			}
			switch {
			case tc.known != nil && !proto.Equal(got.GetKnown(), tc.known):
				t.Errorf("state %v, want known %v", got.State, tc.known)
			case tc.unreadble != nil && !proto.Equal(got.GetUnreadable(), tc.unreadble):
				t.Errorf("state %v, want unreadable %v", got.State, tc.unreadble)
			}
			if err := protovalidate.Validate(got); err != nil {
				t.Errorf("the mapped place is not valid: %v", err)
			}
		})
	}
}

// Every placement the CLI contract allows maps to a valid eval place: the two contracts stay in step
// over the whole input space, not only over the cases above.
func TestEveryValidPlacementViewMapsToAValidEvalPlace(t *testing.T) {
	vendors := []cli.AcceleratorVendor{cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorVendor_ACCELERATOR_VENDOR_AWS, cli.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE}
	models := map[cli.AcceleratorVendor][]cli.AcceleratorModel{
		cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA: {cli.AcceleratorModel_ACCELERATOR_MODEL_H100, cli.AcceleratorModel_ACCELERATOR_MODEL_B200, cli.AcceleratorModel_ACCELERATOR_MODEL_L4, cli.AcceleratorModel_ACCELERATOR_MODEL_A100, cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER},
		cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD:    {cli.AcceleratorModel_ACCELERATOR_MODEL_MI210, cli.AcceleratorModel_ACCELERATOR_MODEL_MI250, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300A, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300X, cli.AcceleratorModel_ACCELERATOR_MODEL_MI325X, cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER},
		cli.AcceleratorVendor_ACCELERATOR_VENDOR_AWS:    {cli.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2, cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER},
		cli.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE:  {cli.AcceleratorModel_ACCELERATOR_MODEL_APPLE_SILICON, cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER},
	}
	kinds := []cli.PlacementKind{cli.PlacementKind_PLACEMENT_KIND_LOCAL, cli.PlacementKind_PLACEMENT_KIND_SSH, cli.PlacementKind_PLACEMENT_KIND_SLURM, cli.PlacementKind_PLACEMENT_KIND_APPTAINER}
	reasons := []cli.OffersUnknownReason{cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_UNREACHABLE, cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_UNREADABLE, cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_TIMED_OUT, cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_NOT_PROBEABLE}
	for seed := range 3000 {
		r := rand.New(rand.NewSource(int64(seed)))
		view := &cli.PlacementView{Name: "place-" + string(rune('a'+r.Intn(26))), Kind: kinds[r.Intn(len(kinds))], MaxJobs: uint32(r.Intn(9))}
		state := []cli.ReachState{cli.ReachState_REACH_STATE_REACHABLE, cli.ReachState_REACH_STATE_UNREACHABLE, cli.ReachState_REACH_STATE_NOT_PROBED}[r.Intn(3)]
		why := ""
		if state == cli.ReachState_REACH_STATE_UNREACHABLE {
			why = "no route"
		}
		view.Reach = reach(state, why)
		if r.Intn(3) == 0 {
			view.Offers = &cli.PlacementView_Unknown{Unknown: &cli.OffersUnknown{Reason: reasons[r.Intn(len(reasons))], Detail: "x"}}
		} else {
			var offers []*cli.AcceleratorOffer
			for range r.Intn(4) {
				v := vendors[r.Intn(len(vendors))]
				m := models[v][r.Intn(len(models[v]))]
				offers = append(offers, offer(v, m, uint32(1+r.Intn(16)), "device"))
			}
			source := cli.OfferSource_OFFER_SOURCE_PROBED
			var at *timestamppb.Timestamp = probedAt
			if r.Intn(2) == 0 {
				source, at = cli.OfferSource_OFFER_SOURCE_DECLARED, nil
			}
			view.Offers = known(source, at, offers...)
		}
		if err := protovalidate.Validate(view); err != nil {
			t.Fatalf("seed %d: the generator made an invalid view: %v", seed, err)
		}
		got, err := clidaemon.EvalPlace(view)
		if err != nil {
			t.Fatalf("seed %d: %v: %v", seed, view, err)
		}
		if err := protovalidate.Validate(got); err != nil {
			t.Fatalf("seed %d: %v maps to an invalid place %v: %v", seed, view, got, err)
		}
		// An unreachable placement never reports offers a task could fit.
		if state == cli.ReachState_REACH_STATE_UNREACHABLE && (got.GetKnown() != nil || len(got.Offers) != 0) {
			t.Fatalf("seed %d: an unreachable placement maps to %v", seed, got)
		}
	}
}

func TestEvalPlacesAsksTheDaemonAndKeepsItsOrder(t *testing.T) {
	daemon := &fakePlacements{resp: &cli.ListPlacementsResponse{Placements: []*cli.PlacementView{
		{Name: "zeta", Kind: cli.PlacementKind_PLACEMENT_KIND_LOCAL, Reach: reach(cli.ReachState_REACH_STATE_REACHABLE, ""), Offers: known(cli.OfferSource_OFFER_SOURCE_PROBED, probedAt)},
		{Name: "alpha", Kind: cli.PlacementKind_PLACEMENT_KIND_SSH, Reach: reach(cli.ReachState_REACH_STATE_UNREACHABLE, "down"), Offers: known(cli.OfferSource_OFFER_SOURCE_PROBED, probedAt)},
	}}}
	for _, refresh := range []bool{false, true} {
		places, err := clidaemon.EvalPlaces(context.Background(), daemon, refresh)
		if err != nil || len(places) != 2 || places[0].Name != "zeta" || places[1].Name != "alpha" {
			t.Fatalf("places %v, %v", places, err)
		}
	}
	if daemon.refreshes != 1 {
		t.Errorf("%d refreshes asked, want the one that said so", daemon.refreshes)
	}
	resp := &pb.ListEvalPlacesResponse{}
	places, _ := clidaemon.EvalPlaces(context.Background(), daemon, false)
	resp.Places = places
	if err := protovalidate.Validate(resp); err != nil {
		t.Errorf("the response is not valid: %v", err)
	}
}

func TestAnInvalidReportOrAFailedCallIsAnErrorNamingTheFault(t *testing.T) {
	bad := &cli.ListPlacementsResponse{Placements: []*cli.PlacementView{{Name: "", Kind: cli.PlacementKind_PLACEMENT_KIND_LOCAL}}}
	if _, err := clidaemon.EvalPlaces(context.Background(), &fakePlacements{resp: bad}, false); err == nil {
		t.Error("a report that breaks the CLI contract was accepted")
	}
	failure := errors.New("daemon is gone")
	if _, err := clidaemon.EvalPlaces(context.Background(), &fakePlacements{err: failure}, false); !errors.Is(err, failure) {
		t.Errorf("got %v, want the call's own error", err)
	}
	// A kind the orchestrator has no place kind for is named, not dropped.
	view := &cli.PlacementView{Name: "weird", Kind: cli.PlacementKind(99), Reach: reach(cli.ReachState_REACH_STATE_REACHABLE, ""), Offers: known(cli.OfferSource_OFFER_SOURCE_PROBED, probedAt)}
	if _, err := clidaemon.EvalPlace(view); err == nil || !strings.Contains(err.Error(), "weird") {
		t.Errorf("got %v, want an error naming the placement", err)
	}
}

func TestPlacesEnsuresConnectsListsAndCloses(t *testing.T) {
	daemon := &fakePlacements{resp: &cli.ListPlacementsResponse{Placements: []*cli.PlacementView{
		{Name: "local", Kind: cli.PlacementKind_PLACEMENT_KIND_LOCAL, Reach: reach(cli.ReachState_REACH_STATE_REACHABLE, ""), Offers: known(cli.OfferSource_OFFER_SOURCE_PROBED, probedAt)},
	}}}
	socket, err := clidaemon.SocketFrom("/run/test/daemon.sock")
	if err != nil {
		t.Fatal(err)
	}
	places := clidaemon.Places{
		Ensurer: ensurer{socket: socket},
		Connect: func(_ context.Context, got clidaemon.Socket) (clidaemon.PlacementConnection, error) {
			if got != socket {
				t.Errorf("connected to %v, want the socket the tool answered", got)
			}
			return daemon, nil
		},
	}
	got, err := places.List(context.Background(), true)
	if err != nil || len(got) != 1 || daemon.closed != 1 || daemon.refreshes != 1 {
		t.Fatalf("places %v, err %v, closed %d, refreshes %d", got, err, daemon.closed, daemon.refreshes)
	}
	gone := errors.New("no tool")
	if _, err := (clidaemon.Places{Ensurer: ensurer{err: gone}}).List(context.Background(), false); !errors.Is(err, gone) {
		t.Errorf("got %v, want the ensurer's error", err)
	}
}

type ensurer struct {
	socket clidaemon.Socket
	err    error
}

func (e ensurer) Ensure(context.Context) (clidaemon.Socket, error) { return e.socket, e.err }
