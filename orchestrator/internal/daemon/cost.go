package daemon

import (
	"errors"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// CostRefresh is how long a watcher of a run that is not over goes without a new view when nothing
// happens in the run: the view is as of a time, so a rate and the end of the last bucket move with it.
const CostRefresh = time.Minute

// CostWatch is a WatchRunCost stream.
type CostWatch = watch[*pb.WatchRunCostResponse]

// costFeed is what one WatchRunCost stream keeps: the ledger of the run's finished turns, which it
// reads from the store when the stream starts and from then on adds to as turns end, so that a view
// costs the size of the view and not of the run.
type costFeed struct {
	s      *Service
	run    core.RunID
	ledger *cost.Ledger
	// dirty says the ledger took something since the last view was sent.
	dirty bool
	// over says the last view sent was of a run that was over, so that no later time changes it.
	over bool
	// sentAt is the time of the last view.
	sentAt time.Time
}

// WatchRunCostFrom opens the stream behind WatchRunCost, for callers that poll it. The first
// response is the whole view; each later one replaces it, one for the events of a poll, and one
// every CostRefresh while the run is live.
func (s *Service) WatchRunCostFrom(run core.RunID) (*CostWatch, []*pb.WatchRunCostResponse, error) {
	if _, ok := s.e.State().Run(run); !ok {
		return nil, nil, status.Errorf(codes.NotFound, "no run %s: list the runs with WatchRuns", run)
	}
	f := &costFeed{s: s, run: run}
	w := &CostWatch{
		e: s.e, run: run, maxBytes: MaxWatchFrameBytes,
		snapshot: f.snapshot,
		change:   f.change,
		delta:    func(*pb.Delta) (*pb.WatchRunCostResponse, bool) { return nil, false },
		settle:   f.settle,
	}
	first, err := w.open(hub.Interest{Run: run, Ticks: true}, 0)
	return w, first, err
}

// snapshot reads the run's finished turns and works out the first view. It runs where nothing
// commits, so the turns it reads are those of the sequence it reports.
func (f *costFeed) snapshot(state core.State, last uint64) (*pb.WatchRunCostResponse, error) {
	run, ok := state.Run(f.run)
	if !ok {
		return nil, status.Errorf(codes.NotFound, "no run %s", f.run)
	}
	f.ledger = &cost.Ledger{}
	for _, a := range state.Agents(f.run) {
		f.ledger.Agent(a)
	}
	if err := f.s.e.readTurns(state, f.run, f.ledger); err != nil {
		return nil, err
	}
	f.over = run.Status.Final()
	return f.view(run, last), nil
}

// turnSink is what the turns of a run are read into: a run's Ledger, or a Host that counts the turns of
// many runs.
type turnSink interface {
	Agent(core.Agent) bool
	Add(core.Turn, core.Agent)
}

// readTurns adds every turn of a run to a sink.
func (e *Engine) readTurns(state core.State, run core.RunID, into turnSink) error {
	return e.readTurnsAfter(state, run, time.Time{}, into)
}

// readTurnsAfter adds the turns of a run that ended after from to a sink, a window at a time; the zero
// time reads them all. A window holds the turns that overlap it, so each follows from the start of the
// last turn of the one before, and the few turns the two share are added twice, which a ledger counts
// once. A window that holds nothing past the one before would be asked for forever, which more
// concurrent turns than a window holds would cause and no run has, so it is an error and not a loop.
func (e *Engine) readTurnsAfter(state core.State, run core.RunID, from time.Time, into turnSink) error {
	var reached *core.Turn
	for {
		window, err := e.Store.Turns(run, from, endOfTime, store.MaxWindowTurns)
		if err != nil {
			return fmt.Errorf("read the turns of %s: %w", run, err)
		}
		for _, t := range window.Turns {
			agent, ok := state.Agent(t.Agent)
			if !ok {
				return fmt.Errorf("turn %s belongs to agent %s, which the run does not have", t.ID, t.Agent)
			}
			into.Agent(agent)
			into.Add(t, agent)
		}
		if !window.Truncated {
			return nil
		}
		last := window.Turns[len(window.Turns)-1]
		if reached != nil && !after(last, *reached) {
			return errors.New("read the turns of " + string(run) + ": more turns than a window holds are running at once")
		}
		reached, from = &last, last.StartedAt
	}
}

// after says turn a comes after turn b in the order the store reads them: by start, then by id.
func after(a, b core.Turn) bool {
	return a.StartedAt.After(b.StartedAt) || (a.StartedAt.Equal(b.StartedAt) && a.ID > b.ID)
}

func (f *costFeed) view(run core.Run, sequence uint64) *pb.WatchRunCostResponse {
	now := f.s.e.Clock.Now()
	f.sentAt, f.dirty = now, false
	return &pb.WatchRunCostResponse{Cost: wire.CostMessage(f.ledger.View(run, now), sequence)}
}

// change adds what an event of the run says to the ledger, and sends nothing: settle sends one view
// for all the events of a poll. A turn that ended counts; an agent the ledger has not met counts as
// one of its type; the run's end fixes the view's time.
func (f *costFeed) change(c *pb.RunChange) (*pb.WatchRunCostResponse, bool) {
	switch b := c.GetBody().(type) {
	case *pb.RunChange_Turn:
		turn, err := wire.TurnFromMessage(b.Turn)
		if err != nil || turn.Status == core.TurnRunning {
			return nil, false
		}
		if agent, ok := f.s.e.State().Agent(turn.Agent); ok {
			f.ledger.Agent(agent)
			f.ledger.Add(turn, agent)
			f.dirty = true
		}
	case *pb.RunChange_Agent:
		if agent, ok := f.s.e.State().Agent(core.AgentID(b.Agent.GetId())); ok && f.ledger.Agent(agent) {
			f.dirty = true
		}
	case *pb.RunChange_Run:
		if run, ok := f.s.e.State().Run(f.run); ok && run.Status.Final() && !f.over {
			f.dirty = true
		}
	}
	return nil, false
}

// settle sends the view when an event changed it, and a fresh one when the run is live and the last
// is a refresh old. Once a run is over its view no longer moves with the time, so only an event
// sends another.
func (f *costFeed) settle(sent uint64) []*pb.WatchRunCostResponse {
	run, ok := f.s.e.State().Run(f.run)
	if !ok || (!f.dirty && (f.over || f.s.e.Clock.Now().Sub(f.sentAt) < CostRefresh)) {
		return nil
	}
	f.over = run.Status.Final()
	return []*pb.WatchRunCostResponse{f.view(run, sent)}
}

// WatchRunCost streams what one run has cost: its whole cost view, then the whole view again each
// time it changes.
func (s *Service) WatchRunCost(req *pb.WatchRunCostRequest, stream grpc.ServerStreamingServer[pb.WatchRunCostResponse]) error {
	caller, err := s.caller(stream.Context())
	if err != nil {
		return err
	}
	if err := wire.Validate(req); err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	if err := operatorOnly(caller, "WatchRunCost"); err != nil {
		return err
	}
	w, first, err := s.WatchRunCostFrom(core.RunID(req.GetRunId()))
	if err != nil {
		return watchError(err)
	}
	defer w.Close()
	return serve(stream.Context(), w, first, stream.Send)
}
