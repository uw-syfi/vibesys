package daemon

import (
	"context"
	"errors"
	"sort"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/fit"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// CatalogueSource serves the example tasks of the release the host runs, without their fit, which the
// daemon works out from the places.
type CatalogueSource interface {
	Catalogue(ctx context.Context) (Catalogue, error)
}

// Catalogue is what a CatalogueSource holds.
type Catalogue struct {
	Release string
	// RepositoriesDirectory is where Provisioner puts a repository when the request names no parent.
	RepositoriesDirectory string
	// Entries carry no fit.
	Entries []*pb.CatalogueEntry
}

// ListCatalogue implements OrchestratorService: the example tasks, grouped by domain (the domains in
// the contract's order, the source's order within one), each with its fit over the eval places as they
// are now. It answers Unimplemented on a host with no catalogue; when the catalogue cannot be read it
// answers Unavailable, unless the cause is one catalogueProblem classifies (no release, or an
// incomplete one), which it reports instead as a successful reply naming the problem.
//
// The fit is advice. Places that cannot be asked make every entry PLACES_UNREADABLE rather than fail the
// call: the catalogue is still worth showing, and StartRun decides the fit again.
func (s *Service) ListCatalogue(ctx context.Context, req *pb.ListCatalogueRequest) (*pb.ListCatalogueResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "ListCatalogue"); err != nil {
		return nil, err
	}
	if s.Catalogue == nil {
		return nil, unimplemented("ListCatalogue")
	}
	catalogue, err := bound(s.Bounds, ctx, s.Catalogue.Catalogue)
	if err != nil {
		problem := catalogueProblem(err)
		if problem == nil {
			return nil, status.Error(codes.Unavailable, err.Error())
		}
		resp := &pb.ListCatalogueResponse{RepositoriesDirectory: catalogue.RepositoriesDirectory, Problem: problem}
		if err := wire.Validate(resp); err != nil {
			return nil, status.Errorf(codes.Internal, "the host's catalogue is not valid: %v", err)
		}
		return resp, nil
	}
	var places []*pb.EvalPlace
	if s.Places != nil {
		var err error
		places, err = bound(s.Bounds, ctx, func(c context.Context) ([]*pb.EvalPlace, error) { return s.Places.Places(c, false) })
		if err != nil {
			places = nil
		}
	}
	resp := &pb.ListCatalogueResponse{
		Release: catalogue.Release, RepositoriesDirectory: catalogue.RepositoriesDirectory,
		Entries: withFit(catalogue.Entries, places),
	}
	if err := wire.Validate(resp); err != nil {
		return nil, status.Errorf(codes.Internal, "the host's catalogue is not valid: %v", err)
	}
	return resp, nil
}

// withFit copies the entries, grouped by domain, each with the fit of its card over the places. No
// place at all, or a failure to ask, is the same as no readable place.
func withFit(entries []*pb.CatalogueEntry, places []*pb.EvalPlace) []*pb.CatalogueEntry {
	out := make([]*pb.CatalogueEntry, len(entries))
	for i, entry := range entries {
		out[i] = proto.Clone(entry).(*pb.CatalogueEntry)
		out[i].Fit = fit.Of(entry.GetCard().GetHardware(), places)
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].GetCard().GetDomain() < out[j].GetCard().GetDomain() })
	return out
}

// catalogueProblem classifies the two causes a client can act on (no release at all, or one missing
// files), the same types production.problemReason reads for HostControlService.Identify's parts. Any
// other failure (a corrupt catalogue.pb, a places read that cannot run) is nil: ListCatalogue keeps
// answering Unavailable for it, unchanged.
func catalogueProblem(err error) *pb.CatalogueProblem {
	var notInstalled *release.NotInstalledError
	var incomplete *release.IncompleteError
	switch {
	case errors.As(err, &notInstalled):
		return &pb.CatalogueProblem{Reason: pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_NO_RELEASE, Detail: err.Error()}
	case errors.As(err, &incomplete):
		return &pb.CatalogueProblem{Reason: pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_RELEASE_INCOMPLETE, Detail: err.Error()}
	default:
		return nil
	}
}
