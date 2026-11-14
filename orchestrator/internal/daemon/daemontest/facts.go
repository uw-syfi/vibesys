package daemontest

import (
	"context"
	"sync"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// FakeTaskReader is a daemon.TaskReader over cards a test sets. A task it holds no card for is
// unreadable as not found, naming the task, as the real reader answers.
type FakeTaskReader struct {
	mu    sync.Mutex
	cards map[string]core.TaskCard
	fail  map[string]error
	calls []daemon.TaskRef
}

var _ daemon.TaskReader = (*FakeTaskReader)(nil)

// Set makes the task read as card, whatever the source.
func (f *FakeTaskReader) Set(task string, card core.TaskCard) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.cards == nil {
		f.cards = map[string]core.TaskCard{}
	}
	f.cards[task] = card
}

// Fail makes the task's read fail with err.
func (f *FakeTaskReader) Fail(task string, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail == nil {
		f.fail = map[string]error{}
	}
	f.fail[task] = err
}

// Calls lists the reads made, in order.
func (f *FakeTaskReader) Calls() []daemon.TaskRef {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]daemon.TaskRef(nil), f.calls...)
}

// Read implements daemon.TaskReader.
func (f *FakeTaskReader) Read(_ context.Context, ref daemon.TaskRef) (core.TaskCard, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, ref)
	if err := f.fail[ref.Task]; err != nil {
		return core.TaskCard{}, err
	}
	if card, ok := f.cards[ref.Task]; ok {
		return card, nil
	}
	return core.TaskCard{}, &daemon.TaskUnreadable{Reason: daemon.TaskNotFound, Detail: "task " + ref.Task + " has no directory"}
}

// FakePlaceSource is a daemon.PlaceSource over places a test sets. It counts the probes asked for.
type FakePlaceSource struct {
	mu        sync.Mutex
	places    []*pb.EvalPlace
	err       error
	refreshes int
}

var _ daemon.PlaceSource = (*FakePlaceSource)(nil)

// Set makes the source report places.
func (f *FakePlaceSource) Set(places ...*pb.EvalPlace) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.places = places
}

// Fail makes every later call fail with err; nil stops.
func (f *FakePlaceSource) Fail(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.err = err
}

// Refreshes counts the calls that asked to probe.
func (f *FakePlaceSource) Refreshes() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.refreshes
}

// Places implements daemon.PlaceSource.
func (f *FakePlaceSource) Places(_ context.Context, refresh bool) ([]*pb.EvalPlace, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if refresh {
		f.refreshes++
	}
	return f.places, f.err
}

// FakeCatalogueSource is a daemon.CatalogueSource over a catalogue a test sets.
type FakeCatalogueSource struct {
	mu        sync.Mutex
	catalogue daemon.Catalogue
	err       error
	problem   *pb.CatalogueProblem
}

var _ daemon.CatalogueSource = (*FakeCatalogueSource)(nil)

// Set makes the source serve c.
func (f *FakeCatalogueSource) Set(c daemon.Catalogue) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.catalogue = c
}

// Fail makes every later call fail with err; nil stops.
func (f *FakeCatalogueSource) Fail(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.err = err
}

// Classify makes Problem answer p for a failure the client can act on; nil keeps Unavailable. It
// stands in for the real source's reading of the release errors, which this layer may not import.
func (f *FakeCatalogueSource) Classify(p *pb.CatalogueProblem) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.problem = p
}

// Catalogue implements daemon.CatalogueSource.
func (f *FakeCatalogueSource) Catalogue(context.Context) (daemon.Catalogue, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.catalogue, f.err
}

// Problem implements daemon.CatalogueSource: the problem set by Classify, independent of the error.
func (f *FakeCatalogueSource) Problem(error) *pb.CatalogueProblem {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.problem
}

// FakeProvisioner is a daemon.Provisioner that makes no repository. It answers each request with the
// outcome a test set for the name, or provisions it as its own path under the parent, and remembers
// what it was asked.
type FakeProvisioner struct {
	mu       sync.Mutex
	outcomes map[string]*pb.ProvisionRepositoryResponse
	calls    []*pb.ProvisionRepositoryRequest
}

var _ daemon.Provisioner = (*FakeProvisioner)(nil)

// Answer makes a request for name answer with response.
func (f *FakeProvisioner) Answer(name string, response *pb.ProvisionRepositoryResponse) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.outcomes == nil {
		f.outcomes = map[string]*pb.ProvisionRepositoryResponse{}
	}
	f.outcomes[name] = response
}

// Calls lists the requests made, in order.
func (f *FakeProvisioner) Calls() []*pb.ProvisionRepositoryRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]*pb.ProvisionRepositoryRequest(nil), f.calls...)
}

// Provision implements daemon.Provisioner.
func (f *FakeProvisioner) Provision(_ context.Context, req *pb.ProvisionRepositoryRequest) (*pb.ProvisionRepositoryResponse, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, req)
	if out, ok := f.outcomes[req.GetName()]; ok {
		return out, nil
	}
	parent := req.GetParentDirectory()
	if parent == "" {
		parent = "/repositories"
	}
	return &pb.ProvisionRepositoryResponse{Outcome: &pb.ProvisionRepositoryResponse_Provisioned{
		Provisioned: &pb.Provisioned{Repository: parent + "/" + req.GetName(), Commit: "0123456789012345678901234567890123456789"},
	}}, nil
}
