package daemon

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// searchState is called with the engine lock held. A cached revision makes the
// health transition time stable across snapshots and unchanged ticks.
func (e *Engine) searchState(run core.RunID) *pb.SearchState {
	v := search.View{Reason: search.NoDaemon}
	var revision uint64
	if e.Search != nil {
		v, revision = e.Search.Get(run)
		if revision == 0 {
			v.Reason = search.NoDaemon
		}
	}
	// Reserve the first revision for a run that has not received a CLI view yet.
	revision++
	old := e.searches[run]
	// Reconciliation may release a finished run's store entry. Its last view
	// remains available to watchers for the lifetime of this daemon.
	if revision == 1 && old != nil {
		return old
	}
	if old != nil && old.Revision == revision {
		return old
	}
	s := search.Cap(v)
	s.RunId = string(run)
	s.Revision = revision
	at := timestamppb.New(e.Clock.Now())
	if v.Reason == search.Readable {
		s.Health = &pb.SearchHealth{State: &pb.SearchHealth_LiveSince{LiveSince: at}}
		if old != nil && old.Health.GetLiveSince() != nil {
			s.Health = old.Health
		}
	} else {
		reason := unreadableReason(v.Reason)
		if old != nil && old.Health.GetUnreadable().GetReason() == reason {
			at = old.Health.GetUnreadable().Since
		}
		s.Health = &pb.SearchHealth{State: &pb.SearchHealth_Unreadable{Unreadable: &pb.Unreadable{Reason: reason, Since: at, Detail: v.Detail}}}
	}
	e.searches[run] = s
	return s
}

func unreadableReason(r search.Reason) pb.UnreadableReason {
	switch r {
	case search.NoDaemon, search.Connecting:
		return pb.UnreadableReason_UNREADABLE_REASON_NO_DAEMON
	case search.StartFailed:
		return pb.UnreadableReason_UNREADABLE_REASON_START_FAILED
	case search.CLINotInstalled:
		return pb.UnreadableReason_UNREADABLE_REASON_CLI_NOT_INSTALLED
	case search.ClientTooOld:
		return pb.UnreadableReason_UNREADABLE_REASON_CLIENT_TOO_OLD
	case search.DaemonTooOld:
		return pb.UnreadableReason_UNREADABLE_REASON_DAEMON_TOO_OLD
	case search.StoreOwnedElsewhere:
		return pb.UnreadableReason_UNREADABLE_REASON_STORE_OWNED_ELSEWHERE
	case search.StoreAway:
		return pb.UnreadableReason_UNREADABLE_REASON_STORE_AWAY
	case search.StreamLost, search.MessageTooLarge:
		return pb.UnreadableReason_UNREADABLE_REASON_STREAM_LOST
	default:
		panic("daemon: unhandled search reason")
	}
}

func (d *Daemon) publishSearch() bool {
	moved := false
	_ = d.Cut(func(state core.State, _ uint64) error {
		for _, r := range state.Runs() {
			s := d.searchState(r.ID)
			if s.Revision > d.searchPublished[r.ID] {
				d.Hub.PublishLatest(r.ID, s)
				d.searchPublished[r.ID] = s.Revision
				moved = true
			}
		}
		return nil
	})
	return moved
}
