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

// TestListCatalogueReportsAClassifiedProblemInsteadOfFailing covers the daemon's handling once the
// source classifies a read failure as a client-actionable problem (no release at all, or one missing
// a file): a successful, valid reply naming the problem, with no entries and the repositories
// directory kept, rather than the opaque Unavailable every unclassified read failure still gets. The
// source owns which release error maps to which reason; that mapping is tested in production, where the
// release layout may be imported.
func TestListCatalogueReportsAClassifiedProblemInsteadOfFailing(t *testing.T) {
	for name, reason := range map[string]pb.CatalogueProblemReason{
		"no release":            pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_NO_RELEASE,
		"an incomplete release": pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_RELEASE_INCOMPLETE,
	} {
		t.Run(name, func(t *testing.T) {
			detail := "the release is " + name
			r := daemontest.NewRig(t)
			r.Catalogue.Set(daemon.Catalogue{RepositoriesDirectory: "/home/user/.vibesys/repos"})
			r.Catalogue.Fail(errors.New("the catalogue cannot be read"))
			r.Catalogue.Classify(&pb.CatalogueProblem{Reason: reason, Detail: detail})
			resp, err := r.Service.ListCatalogue(context.Background(), &pb.ListCatalogueRequest{})
			if err != nil {
				t.Fatalf("got an error, want a classified reply: %v", err)
			}
			if err := wire.Validate(resp); err != nil {
				t.Fatalf("response is not valid: %v", err)
			}
			if resp.GetProblem().GetReason() != reason {
				t.Errorf("reason %v, want %v", resp.GetProblem().GetReason(), reason)
			}
			if resp.GetProblem().GetDetail() != detail {
				t.Errorf("detail %q, want %q", resp.GetProblem().GetDetail(), detail)
			}
			if len(resp.GetEntries()) != 0 {
				t.Errorf("%d entries, want none", len(resp.GetEntries()))
			}
			if resp.GetRepositoriesDirectory() != "/home/user/.vibesys/repos" {
				t.Errorf("repositories directory %q, want it kept from the source", resp.GetRepositoriesDirectory())
			}
		})
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
