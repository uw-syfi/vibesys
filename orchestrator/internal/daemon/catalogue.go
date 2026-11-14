package daemon

import (
	"context"
	"sort"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/fit"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// CatalogueSource serves the example tasks of the release the host runs, without their fit, which the
// daemon works out from the places.
type CatalogueSource interface {
	Catalogue(ctx context.Context) (Catalogue, error)
	// Problem classifies an error from Catalogue into the cause a client can act on (no release at
	// all, or an incomplete one), or nil when the error is not one the client can act on. The daemon
	// keeps the release-layout knowledge out of its layer by asking the source, which owns it.
	Problem(err error) *pb.CatalogueProblem
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
// answers Unavailable, unless the source classifies the cause as a client-actionable problem (no
// release, or an incomplete one), which it reports instead as a successful reply naming the problem.
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
		problem := s.Catalogue.Problem(err)
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
