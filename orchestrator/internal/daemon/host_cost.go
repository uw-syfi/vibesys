package daemon

import (
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// HostCostWatch is a WatchHostCost stream.
type HostCostWatch = watch[*pb.WatchHostCostResponse]

// hostCostFeed is what one WatchHostCost stream keeps: a cost.Host that holds the finished turns of
// every run the stream counts, which it reads from the store when the stream starts and from then on
// adds to as turns end, as a WatchRunCost stream does for one run. A view costs the size of the
// view and the turns the host holds, and the store is read once.
type hostCostFeed struct {
	s     *Service
	since time.Time
	host  *cost.Host
	// dirty says the host took something since the last view was sent.
	dirty bool
	// sentAt is the time of the last view.
	sentAt time.Time
}

// WatchHostCostFrom opens the stream behind WatchHostCost, for callers that poll it. The first
// response is the whole view of every run; each later one replaces it, one for the events of a poll
// that changed it, and one every CostRefresh while a counted run is live. The zero since counts
// everything the host holds. A since after the host's clock is refused, so that a window is always a
// span of the past and a run that ends in it is never one the stream failed to count.
func (s *Service) WatchHostCostFrom(since time.Time) (*HostCostWatch, []*pb.WatchHostCostResponse, error) {
	if since.After(s.e.Clock.Now()) {
		return nil, nil, status.Errorf(codes.InvalidArgument, "since %s is after the host's clock", since.UTC().Format(time.RFC3339))
	}
	f := &hostCostFeed{s: s, since: since}
	w := &HostCostWatch{
		e: s.e, maxBytes: MaxWatchFrameBytes,
		snapshot: f.snapshot,
		change:   f.change,
		delta:    func(*pb.Delta) (*pb.WatchHostCostResponse, bool) { return nil, false },
		settle:   f.settle,
	}
	first, err := w.open(hub.Interest{Everything: true, Ticks: true}, 0)
	return w, first, err
}

// snapshot reads the turns of every run the stream counts and works out the first view. It runs where
// nothing commits, so the turns it reads are those of the sequence it reports. A run that ended before
// the window is not read at all, and a counted run is read from the turns that overlap it.
func (f *hostCostFeed) snapshot(state core.State, last uint64) (*pb.WatchHostCostResponse, error) {
	f.host = cost.NewHost(f.since)
	for _, run := range state.Runs() {
		if counted, _ := f.host.Run(run); !counted {
			continue
		}
		for _, a := range state.Agents(run.ID) {
			f.host.Agent(a)
		}
		if err := f.s.e.readTurnsAfter(state, run.ID, f.readFrom(), f.host); err != nil {
			return nil, err
		}
	}
	return f.view(last), nil
}

// readFrom is where in a run the store is read: a turn that ended at or after the window's start ended
// after the instant before it. A turn that overlaps the read and ended before the window is left out
// by the host.
func (f *hostCostFeed) readFrom() time.Time {
	if f.since.IsZero() {
		return time.Time{}
	}
	return f.since.Add(-time.Nanosecond)
}

func (f *hostCostFeed) view(sequence uint64) *pb.WatchHostCostResponse {
	now := f.s.e.Clock.Now()
	f.sentAt, f.dirty = now, false
	return &pb.WatchHostCostResponse{Cost: wire.HostCostMessage(f.host.View(now), sequence)}
}

// change adds what an event of any run says to the host, and sends nothing: settle sends one view for
// all the events of a poll. A turn that ended counts; an agent the host has not met counts as one of
// its type; a run that began or whose status moved changes the line it has.
func (f *hostCostFeed) change(c *pb.RunChange) (*pb.WatchHostCostResponse, bool) {
	state := f.s.e.State()
	switch b := c.GetBody().(type) {
	case *pb.RunChange_Turn:
		turn, err := wire.TurnFromMessage(b.Turn)
		if err != nil || turn.Status == core.TurnRunning {
			return nil, false
		}
		if agent, ok := state.Agent(turn.Agent); ok {
			f.host.Agent(agent)
			f.host.Add(turn, agent)
			f.dirty = true
		}
	case *pb.RunChange_Agent:
		if agent, ok := state.Agent(core.AgentID(b.Agent.GetId())); ok && f.host.Agent(agent) {
			f.dirty = true
		}
	case *pb.RunChange_Run:
		if run, ok := state.Run(core.RunID(b.Run.GetId())); ok {
			if _, changed := f.host.Run(run); changed {
				f.dirty = true
			}
		}
	}
	return nil, false
}

// settle sends the view when an event changed it, and a fresh one when a counted run is live and the
// last is a refresh old. With nothing live the view no longer moves with the time, so only an event
// sends another.
func (f *hostCostFeed) settle(sent uint64) []*pb.WatchHostCostResponse {
	if !f.dirty && (!f.host.Live() || f.s.e.Clock.Now().Sub(f.sentAt) < CostRefresh) {
		return nil
	}
	return []*pb.WatchHostCostResponse{f.view(sent)}
}

// WatchHostCost streams what every run the host holds has cost: the whole cost view, then the whole
// view again each time any run's cost changes.
func (s *Service) WatchHostCost(req *pb.WatchHostCostRequest, stream grpc.ServerStreamingServer[pb.WatchHostCostResponse]) error {
	caller, err := s.caller(stream.Context())
	if err != nil {
		return err
	}
	if err := wire.Validate(req); err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	if err := operatorOnly(caller, "WatchHostCost"); err != nil {
		return err
	}
	var since time.Time
	if req.GetSince() != nil {
		if err := req.GetSince().CheckValid(); err != nil {
			return status.Errorf(codes.InvalidArgument, "since: %v", err)
		}
		since = req.GetSince().AsTime()
	}
	w, first, err := s.WatchHostCostFrom(since)
	if err != nil {
		return watchError(err)
	}
	defer w.Close()
	return serve(stream.Context(), w, first, stream.Send)
}
