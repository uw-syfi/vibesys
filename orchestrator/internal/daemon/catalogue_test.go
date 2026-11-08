package daemon_test

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

const (
	amd    = cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD
	nvidia = cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA
	aws    = cli.AcceleratorVendor_ACCELERATOR_VENDOR_AWS
	cpu    = cli.AcceleratorVendor_ACCELERATOR_VENDOR_CPU
)

func entry(example string, domain cli.TaskDomain, need *cli.AcceleratorNeed, clone bool) *pb.CatalogueEntry {
	return &pb.CatalogueEntry{Example: example, NeedsClone: clone, Card: &cli.TaskCard{
		Task: lastPart(example), Domain: domain, Hardware: need,
	}}
}

func lastPart(example string) string {
	for i := len(example) - 1; i >= 0; i-- {
		if example[i] == '/' {
			return example[i+1:]
		}
	}
	return example
}

// The example tasks of the release, as the registry lists them: in registry order, which is not
// grouped, with the accelerators their cards need.
func exampleEntries() []*pb.CatalogueEntry {
	return []*pb.CatalogueEntry{
		entry("data-structures/kvstore-default", cli.TaskDomain_TASK_DOMAIN_DATA_STRUCTURES, &cli.AcceleratorNeed{Vendor: cpu, Count: 1}, false),
		entry("model-serving/flint", cli.TaskDomain_TASK_DOMAIN_LLM_SERVING, &cli.AcceleratorNeed{Vendor: amd, Model: cli.AcceleratorModel_ACCELERATOR_MODEL_MI300X, Count: 8}, false),
		entry("database/sqlite-bench", cli.TaskDomain_TASK_DOMAIN_DATABASES, nil, true),
		entry("model-serving/llama-3-8b", cli.TaskDomain_TASK_DOMAIN_LLM_SERVING, &cli.AcceleratorNeed{Vendor: nvidia, Count: 1}, false),
		entry("model-serving/llama-3-8b-trn2", cli.TaskDomain_TASK_DOMAIN_LLM_SERVING, &cli.AcceleratorNeed{Vendor: aws, Model: cli.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2, Count: 2}, false),
		entry("data-structures/ordered-map", cli.TaskDomain_TASK_DOMAIN_DATA_STRUCTURES, nil, false),
		entry("microservices/shop", cli.TaskDomain_TASK_DOMAIN_MICROSERVICES, &cli.AcceleratorNeed{Vendor: nvidia, Model: cli.AcceleratorModel_ACCELERATOR_MODEL_B200, Count: 1}, false),
		entry("model-serving/mystery", cli.TaskDomain_TASK_DOMAIN_LLM_SERVING, &cli.AcceleratorNeed{Vendor: nvidia, Model: cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER, Count: 1}, false),
	}
}

func place(name string, kind pb.EvalPlaceKind, offers ...*pb.EvalOffer) *pb.EvalPlace {
	return &pb.EvalPlace{Name: name, Kind: kind, Offers: offers, State: &pb.EvalPlace_Known{Known: &pb.OffersKnown{
		Source: pb.OffersSource_OFFERS_SOURCE_PROBED, ProbedAt: timestamppb.New(time.Date(2026, 11, 7, 12, 0, 0, 0, time.UTC))}}}
}

func dark(name string) *pb.EvalPlace {
	return &pb.EvalPlace{Name: name, Kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH, State: &pb.EvalPlace_Unreadable{
		Unreadable: &pb.OffersUnreadable{Reason: pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_UNREACHABLE, Detail: "no route"}}}
}

func gpus(vendor cli.AcceleratorVendor, model cli.AcceleratorModel, count uint32) *pb.EvalOffer {
	return &pb.EvalOffer{Vendor: vendor, Model: model, Count: count, DeviceName: "device"}
}

func listCatalogue(t *testing.T, r *daemontest.Rig) *pb.ListCatalogueResponse {
	t.Helper()
	resp, err := r.Service.ListCatalogue(context.Background(), &pb.ListCatalogueRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if err := wire.Validate(resp); err != nil {
		t.Fatalf("response is not valid: %v", err)
	}
	return resp
}

func byExample(resp *pb.ListCatalogueResponse) map[string]*pb.Fit {
	out := map[string]*pb.Fit{}
	for _, e := range resp.Entries {
		out[e.Example] = e.Fit
	}
	return out
}

func TestListCatalogueGroupsByDomainAndKeepsRegistryOrderWithinOne(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Catalogue.Set(daemon.Catalogue{Release: "1.2.3", RepositoriesDirectory: "/home/user/.vibesys/repos", Entries: exampleEntries()})
	r.Places.Set(place("local", pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL))
	resp := listCatalogue(t, r)
	if resp.Release != "1.2.3" || resp.RepositoriesDirectory != "/home/user/.vibesys/repos" {
		t.Errorf("release %q directory %q", resp.Release, resp.RepositoriesDirectory)
	}
	var domains []cli.TaskDomain
	var order []string
	for _, e := range resp.Entries {
		if len(domains) == 0 || domains[len(domains)-1] != e.Card.Domain {
			domains = append(domains, e.Card.Domain)
		}
		order = append(order, e.Example)
	}
	if !slices.IsSorted(domains) || len(slices.Compact(slices.Clone(domains))) != len(domains) {
		t.Errorf("domains %v: each must form one group, in the contract's order", domains)
	}
	want := []string{
		"model-serving/flint", "model-serving/llama-3-8b", "model-serving/llama-3-8b-trn2", "model-serving/mystery",
		"data-structures/kvstore-default", "data-structures/ordered-map", "database/sqlite-bench", "microservices/shop",
	}
	if !slices.Equal(order, want) {
		t.Errorf("order %v, want %v", order, want)
	}
	if len(resp.Entries) != len(exampleEntries()) {
		t.Errorf("%d entries, want all %d", len(resp.Entries), len(exampleEntries()))
	}
}

// An example whose card says the job tool cannot run it is UNSUPPORTED over every set of places: places
// that give what it needs, places that give nothing, places that cannot be read, and no place at all.
// The state is the card's alone, so a host with a place that gives everything still offers no such
// example as one a run can start from, and one that can run it is as before. The same table is the
// class: for every entry of the catalogue, FITS implies that the card carries no refusal.
func TestAnExampleTheJobToolCannotRunIsUnsupportedOverEveryPlace(t *testing.T) {
	entries := exampleEntries()
	for _, e := range entries {
		e.Card.Unsupported = ""
	}
	// Every other entry cannot be run by this build.
	for i, e := range entries {
		if i%2 == 1 {
			e.Card.Unsupported = "vibesys.input.toml: [evaluator] tasks are not supported by this build; their commands are run by an evaluator package"
		}
	}
	worlds := map[string][]*pb.EvalPlace{
		"every accelerator": {
			place("local", pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL),
			place("host-a", pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH, gpus(nvidia, cli.AcceleratorModel_ACCELERATOR_MODEL_H100, 8), gpus(nvidia, cli.AcceleratorModel_ACCELERATOR_MODEL_B200, 8)),
			place("cluster-a", pb.EvalPlaceKind_EVAL_PLACE_KIND_SLURM, gpus(amd, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300X, 8), gpus(aws, cli.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2, 8)),
		},
		"nothing":    {place("local", pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL)},
		"unreadable": {dark("host-b")},
		"no place":   nil,
	}
	for name, places := range worlds {
		t.Run(name, func(t *testing.T) {
			r := daemontest.NewRig(t)
			r.Catalogue.Set(daemon.Catalogue{Release: "r", RepositoriesDirectory: "/repos", Entries: entries})
			r.Places.Set(places...)
			resp := listCatalogue(t, r)
			if len(resp.Entries) != len(entries) {
				t.Fatalf("%d entries, want %d", len(resp.Entries), len(entries))
			}
			unsupported := 0
			for _, e := range resp.Entries {
				switch {
				case e.Card.GetUnsupported() != "":
					unsupported++
					if !proto.Equal(e.Fit, &pb.Fit{State: pb.FitState_FIT_STATE_UNSUPPORTED}) {
						t.Errorf("%s: fit %v, want UNSUPPORTED and no place", e.Example, e.Fit)
					}
				case e.Fit.State == pb.FitState_FIT_STATE_UNSUPPORTED:
					t.Errorf("%s: UNSUPPORTED for a card that carries no refusal", e.Example)
				}
				if e.Fit.State == pb.FitState_FIT_STATE_FITS && e.Card.GetUnsupported() != "" {
					t.Errorf("%s: offered as fitting a task the job tool refuses: %q", e.Example, e.Card.GetUnsupported())
				}
			}
			if unsupported == 0 || unsupported == len(entries) {
				t.Fatalf("%d of %d entries unsupported: the table has one side", unsupported, len(entries))
			}
		})
	}
}

func TestListCatalogueGivesEachEntryItsFitOverThePlaces(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Catalogue.Set(daemon.Catalogue{Release: "r", RepositoriesDirectory: "/repos", Entries: exampleEntries()})
	r.Places.Set(
		place("local", pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL),
		place("host-a", pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH, gpus(nvidia, cli.AcceleratorModel_ACCELERATOR_MODEL_H100, 8)),
		place("cluster-a", pb.EvalPlaceKind_EVAL_PLACE_KIND_SLURM, gpus(amd, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300X, 8)),
		dark("host-b"),
	)
	fits := byExample(listCatalogue(t, r))
	for example, want := range map[string]*pb.Fit{
		// A CPU need or none fits every place that can be read, and not the one that cannot.
		"data-structures/kvstore-default": {State: pb.FitState_FIT_STATE_FITS, Places: []string{"local", "host-a", "cluster-a"}},
		"database/sqlite-bench":           {State: pb.FitState_FIT_STATE_FITS, Places: []string{"local", "host-a", "cluster-a"}},
		"model-serving/flint":             {State: pb.FitState_FIT_STATE_FITS, Places: []string{"cluster-a"}},
		// "NVIDIA GPU" fits any device of the vendor.
		"model-serving/llama-3-8b": {State: pb.FitState_FIT_STATE_FITS, Places: []string{"host-a"}},
		// No place gives a Trainium or a B200.
		"model-serving/llama-3-8b-trn2": {State: pb.FitState_FIT_STATE_NO_ACCELERATOR},
		"microservices/shop":            {State: pb.FitState_FIT_STATE_NO_ACCELERATOR},
		"model-serving/mystery":         {State: pb.FitState_FIT_STATE_UNKNOWN_ACCELERATOR},
	} {
		if got := fits[example]; !proto.Equal(got, want) {
			t.Errorf("%s: fit %v, want %v", example, got, want)
		}
	}
}

func TestAnUnreadableOrFailingPlaceSourceMakesEveryFitUnreadableAndStillListsTheCatalogue(t *testing.T) {
	for name, arrange := range map[string]func(*daemontest.Rig){
		"every place unreadable": func(r *daemontest.Rig) { r.Places.Set(dark("host-a"), dark("host-b")) },
		"no places":              func(r *daemontest.Rig) { r.Places.Set() },
		"the source fails":       func(r *daemontest.Rig) { r.Places.Fail(errors.New("no CLI daemon")) },
	} {
		t.Run(name, func(t *testing.T) {
			r := daemontest.NewRig(t)
			r.Catalogue.Set(daemon.Catalogue{Release: "r", RepositoriesDirectory: "/repos", Entries: exampleEntries()})
			arrange(r)
			resp := listCatalogue(t, r)
			if len(resp.Entries) != len(exampleEntries()) {
				t.Fatalf("%d entries", len(resp.Entries))
			}
			for _, e := range resp.Entries {
				// Not even a CPU need or no need fits an unreadable place.
				if e.Fit.State != pb.FitState_FIT_STATE_PLACES_UNREADABLE || len(e.Fit.Places) != 0 {
					t.Errorf("%s: fit %v, want PLACES_UNREADABLE", e.Example, e.Fit)
				}
			}
		})
	}
}

func TestListCatalogueDoesNotChangeTheSourceAndRecomputesTheFitOnEveryCall(t *testing.T) {
	r := daemontest.NewRig(t)
	source := exampleEntries()
	r.Catalogue.Set(daemon.Catalogue{Release: "r", RepositoriesDirectory: "/repos", Entries: source})
	r.Places.Set(place("local", pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL))
	if got := byExample(listCatalogue(t, r))["model-serving/flint"]; got.State != pb.FitState_FIT_STATE_NO_ACCELERATOR {
		t.Fatalf("flint on a CPU-only host: %v", got)
	}
	r.Places.Set(place("cluster-a", pb.EvalPlaceKind_EVAL_PLACE_KIND_SLURM, gpus(amd, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300X, 8)))
	if got := byExample(listCatalogue(t, r))["model-serving/flint"]; got.State != pb.FitState_FIT_STATE_FITS {
		t.Fatalf("flint after a cluster joined: %v", got)
	}
	for _, e := range source {
		if e.Fit != nil {
			t.Errorf("the source entry %s was given a fit", e.Example)
		}
	}
	if n := r.Places.Refreshes(); n != 0 {
		t.Errorf("a catalogue call probed %d times: the fit is advice from the last probe", n)
	}
}

func TestListCatalogueFailsWhenTheCatalogueCannotBeRead(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Catalogue.Fail(errors.New("catalogue.pb is missing"))
	if _, err := r.Service.ListCatalogue(context.Background(), &pb.ListCatalogueRequest{}); status.Code(err) != codes.Unavailable {
		t.Errorf("got %v, want Unavailable", err)
	}
}

func TestAHostWithoutSourcesAnswersUnimplementedNamingTheRpc(t *testing.T) {
	r := daemontest.NewRig(t)
	bare, err := daemon.NewService(r.Daemon.Engine, daemon.ServiceDeps{
		Authority: r.Auth, Audience: daemon.AudienceOperator, Bounds: r.Bounds, Binary: "test", Starts: r.Starts,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := bare.ListCatalogue(context.Background(), &pb.ListCatalogueRequest{}); status.Code(err) != codes.Unimplemented {
		t.Errorf("ListCatalogue: %v", err)
	}
	if _, err := bare.ListEvalPlaces(context.Background(), &pb.ListEvalPlacesRequest{}); status.Code(err) != codes.Unimplemented {
		t.Errorf("ListEvalPlaces: %v", err)
	}
}

func TestListEvalPlacesAnswersThePlacesAndPassesRefresh(t *testing.T) {
	r := daemontest.NewRig(t)
	places := []*pb.EvalPlace{
		place("local", pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL),
		dark("host-b"),
	}
	r.Places.Set(places...)
	for _, refresh := range []bool{false, true} {
		resp, err := r.Service.ListEvalPlaces(context.Background(), &pb.ListEvalPlacesRequest{Refresh: refresh})
		if err != nil {
			t.Fatal(err)
		}
		if len(resp.Places) != 2 || !proto.Equal(resp.Places[0], places[0]) || !proto.Equal(resp.Places[1], places[1]) {
			t.Errorf("places %v", resp.Places)
		}
	}
	if r.Places.Refreshes() != 1 {
		t.Errorf("%d refreshes, want the one asked for", r.Places.Refreshes())
	}
	r.Places.Fail(errors.New("no CLI daemon"))
	if _, err := r.Service.ListEvalPlaces(context.Background(), &pb.ListEvalPlacesRequest{}); status.Code(err) != codes.Unavailable {
		t.Errorf("a source that cannot be asked: %v, want Unavailable", err)
	}
}
