package daemon

import (
	"slices"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// MaxFollowUpBytes is how many bytes of frames a stream sends in one burst after its snapshot or its
// replay: the search views and summaries of the runs, in the order the snapshot lists them. It is the
// snapshot's own budget, so the first thing a client is sent is bounded the same way however it is cut
// into frames.
const MaxFollowUpBytes = MaxSnapshotBytes

// runsStream is a WatchRuns stream's account of what its client was told, and the one place that decides
// what it is told next. One goroutine owns it, from open through every Poll and resync.
//
// The rule that holds all of it: a snapshot lists runs in listing order until its byte budget is spent,
// and every run it does not list is left out, for as long as the stream lives. Nothing is sent about a
// left-out run, in any frame, so a client never holds a run it cannot show: the snapshot said how many
// it left out, and a new snapshot (a reconnect) lists again. A run that began after the snapshot is not
// left out, and is sent whole: its change and its summary in the same poll. Follow-up frames are bounded
// by the same order and MaxFollowUpBytes.
type runsStream struct {
	s         *Service
	run       core.RunID
	scoped    bool
	detail    pb.SnapshotDetail
	summaries bool
	// seen is the revision of each search view the client holds.
	seen map[string]uint64
	// held is the summary the client holds of each run.
	held map[string]*pb.RunSummary
	// touched is the runs something happened in since a summary of them was last worked out.
	touched map[core.RunID]bool
	// left is the runs the snapshot left out.
	left map[core.RunID]bool
}

func newRunsStream(s *Service, caller core.Caller, detail pb.SnapshotDetail) *runsStream {
	_, scoped := caller.(core.AgentCaller)
	return &runsStream{
		s: s, run: s.runOf(caller), scoped: scoped, detail: detail,
		summaries: detail == pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY,
		seen:      map[string]uint64{}, held: map[string]*pb.RunSummary{},
		touched: map[core.RunID]bool{}, left: map[core.RunID]bool{},
	}
}

// watch is the generic stream this one drives.
func (st *runsStream) watch() *RunsWatch {
	w := &RunsWatch{
		e: st.s.e, run: st.run, maxBytes: MaxWatchFrameBytes,
		snapshot: st.snapshot, change: st.change, latest: st.latest, current: st.current,
		delta: func(*pb.Delta) (*pb.WatchRunsResponse, bool) { return nil, false },
	}
	if st.summaries {
		w.settle = st.settle
	}
	return w
}

// listed says whether the stream may tell its client about a run: the caller can see it, and the
// snapshot did not leave it out.
func (st *runsStream) listed(id core.RunID) bool {
	return (!st.scoped || id == st.run) && !st.left[id]
}

// touch notes that something happened in a run, so its summary is worked out again.
func (st *runsStream) touch(id core.RunID) {
	if st.summaries {
		st.touched[id] = true
	}
}

func (st *runsStream) change(c *pb.RunChange) (*pb.WatchRunsResponse, bool) {
	if !st.listed(core.RunID(c.GetRunId())) {
		return nil, false
	}
	st.touch(core.RunID(c.GetRunId()))
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Change{Change: c}}, true
}

func (st *runsStream) latest(search *pb.SearchState) (*pb.WatchRunsResponse, bool) {
	id := core.RunID(search.RunId)
	if !st.listed(id) {
		return nil, false
	}
	st.touch(id)
	if search.Revision <= st.seen[search.RunId] {
		return nil, false
	}
	st.seen[search.RunId] = search.Revision
	return searchFrame(search), true
}

func searchFrame(search *pb.SearchState) *pb.WatchRunsResponse {
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Search{Search: search}}
}

func summaryFrame(summary *pb.RunSummary) *pb.WatchRunsResponse {
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Summary{Summary: summary}}
}

// due is what the client does not hold of a run as it now stands: its search view when it has moved on,
// and its summary when it differs from the held one. Nothing is recorded as sent.
func (st *runsStream) due(r core.Run, agents []core.Agent) (search *pb.SearchState, summary *pb.RunSummary) {
	if v := st.s.e.searchState(r.ID); v.Revision > st.seen[string(r.ID)] {
		search = v
	}
	if st.summaries {
		if sum := st.s.e.runSummary(r, agents); !proto.Equal(st.held[string(r.ID)], sum) {
			summary = sum
		}
	}
	return search, summary
}

// current is what the client is owed after a snapshot or a replay: for each listed run, in listing order,
// the frames it does not hold. It stops at the first run whose frames would pass MaxFollowUpBytes and says
// so, so that the sent runs are a prefix of the listing order and the rest are not half-sent.
func (st *runsStream) current(state core.State) (out []*pb.WatchRunsResponse, tooMany bool) {
	used := 0
	runs := state.Runs()
	listingOrder(runs)
	agentsOf := state.AgentsByRun()
	for _, r := range runs {
		if !st.listed(r.ID) {
			continue
		}
		search, summary := st.due(r, agentsOf[r.ID])
		size := 0
		if search != nil {
			size += proto.Size(search)
		}
		if summary != nil {
			size += proto.Size(summary)
		}
		if used > 0 && used+size > MaxFollowUpBytes {
			return out, true
		}
		used += size
		if search != nil {
			st.seen[string(r.ID)] = search.Revision
			out = append(out, searchFrame(search))
		}
		if summary != nil {
			st.held[string(r.ID)] = summary
			out = append(out, summaryFrame(summary))
		}
	}
	clear(st.touched)
	return out, false
}

// settle is one frame for each run the events of a poll touched, after the changes themselves.
func (st *runsStream) settle(uint64) []*pb.WatchRunsResponse {
	if len(st.touched) == 0 {
		return nil
	}
	var out []*pb.WatchRunsResponse
	_ = st.s.e.Cut(func(state core.State, _ uint64) error {
		agentsOf := state.AgentsByRun()
		for _, r := range state.Runs() {
			if !st.touched[r.ID] || !st.listed(r.ID) {
				continue
			}
			sum := st.s.e.runSummary(r, agentsOf[r.ID])
			if proto.Equal(st.held[string(r.ID)], sum) {
				continue
			}
			st.held[string(r.ID)] = sum
			out = append(out, summaryFrame(sum))
		}
		return nil
	})
	clear(st.touched)
	return out
}

// snapshot builds the snapshot of runs. Whatever the detail, a run is listed with its summary or counted
// in runs_left_out, never dropped without a word, and what is cut of a listed run's history sets
// truncated. With detail SUMMARY nothing of a run's history is in it, so its size follows the number of
// runs and not the length of their mailboxes; the summaries and runs are the only part the byte budget
// has to bound. It starts the stream's account again: the revision of each search view in it, the
// summary of each run in it, and the runs it left out.
func (st *runsStream) snapshot(state core.State, last uint64) (*pb.WatchRunsResponse, error) {
	clear(st.touched)
	clear(st.seen)
	clear(st.held)
	clear(st.left)
	snap := &pb.RunsSnapshot{Sequence: last, Models: wire.ModelCatalogMessage(st.s.e.ModelPolicy())}
	var bundles []*pb.RunsSnapshot
	used := proto.Size(snap)
	runs := state.Runs()
	listingOrder(runs)
	agentsOf := state.AgentsByRun()
	var wanted []core.Run
	for _, r := range runs {
		if !st.scoped || r.ID == st.run {
			wanted = append(wanted, r)
		}
	}
	for _, r := range wanted {
		bundle := &pb.RunsSnapshot{Runs: []*pb.Run{wire.RunMessage(r)}, Summaries: []*pb.RunSummary{st.s.e.runSummary(r, agentsOf[r.ID])}}
		if !st.summaries {
			for _, a := range agentsOf[r.ID] {
				bundle.Agents = append(bundle.Agents, wire.AgentMessage(a))
			}
		}
		// A run's identity, summary and agents are listed in preference to anything else of it: if even
		// those do not fit, this run and the ones after it in listing order are left out.
		if used+proto.Size(bundle) > MaxSnapshotBytes-snapshotReserve {
			break
		}
		if !st.summaries {
			st.s.addDetail(bundle, state, r.ID, used, snap)
		}
		bundles = append(bundles, bundle)
		used += proto.Size(bundle)
	}
	for _, r := range wanted[len(bundles):] {
		st.left[r.ID] = true
	}
	snap.RunsLeftOut = uint32(len(wanted) - len(bundles))
	snap.Truncated = snap.Truncated || snap.RunsLeftOut > 0
	slices.SortFunc(bundles, func(a, b *pb.RunsSnapshot) int {
		if a.Runs[0].Id < b.Runs[0].Id {
			return -1
		}
		if a.Runs[0].Id > b.Runs[0].Id {
			return 1
		}
		return 0
	})
	for _, bundle := range bundles {
		snap.Runs = append(snap.Runs, bundle.Runs...)
		snap.Agents = append(snap.Agents, bundle.Agents...)
		snap.Turns = append(snap.Turns, bundle.Turns...)
		snap.Messages = append(snap.Messages, bundle.Messages...)
		snap.Searches = append(snap.Searches, bundle.Searches...)
		snap.CheckIns = append(snap.CheckIns, bundle.CheckIns...)
		snap.Summaries = append(snap.Summaries, bundle.Summaries...)
		for _, search := range bundle.Searches {
			st.seen[search.RunId] = search.Revision
		}
		for _, summary := range bundle.Summaries {
			st.held[summary.RunId] = summary
		}
	}
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: snap}}, nil
}
