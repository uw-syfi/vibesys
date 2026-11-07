package daemon

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// MaxHistoryPageBytes is how many bytes of messages one page of a run's history holds, except that a page
// always holds one message however large, so that a reader can always make progress. It is far below the
// receive limit, so a page and its wrapper never reach it.
const MaxHistoryPageBytes = 1 << 20

// GetRunHistory reads one run's history a page at a time. The first page holds the agents, the turns and
// the check-ins as well as the first messages; each later page holds messages only. A reader that wants
// the run's whole mailbox asks again with each page's next_cursor until it is zero.
func (s *Service) GetRunHistory(ctx context.Context, req *pb.GetRunHistoryRequest) (*pb.GetRunHistoryResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "GetRunHistory"); err != nil {
		return nil, err
	}
	run := core.RunID(req.GetRunId())
	var resp *pb.GetRunHistoryResponse
	err = s.e.Cut(func(state core.State, last uint64) error {
		if _, ok := state.Run(run); !ok {
			return status.Errorf(codes.NotFound, "no run %s: list the runs with WatchRuns", run)
		}
		var err error
		resp, err = s.historyPage(state, run, req.GetCursor())
		if err != nil {
			return err
		}
		resp.Sequence = last
		return nil
	})
	var denied interface{ GRPCStatus() *status.Status }
	switch {
	case err == nil:
		return resp, nil
	case errors.As(err, &denied):
		return nil, err
	case errors.Is(err, store.ErrAheadOfLog):
		return nil, status.Errorf(codes.OutOfRange, "the cursor %d is past the end of the history of %s: start again from the first page", req.GetCursor(), run)
	}
	return nil, status.Error(codes.Unavailable, err.Error())
}

// pageOverhead is what a page's own fields (its sequence, cursor, counts and flags) take beyond its
// elements, at their widest.
const pageOverhead = 64

// elementSize is what a message takes as an element of a repeated field of a page: its tag, its length and
// its bytes. The fields of a page that hold elements are numbered below 16, so a tag is one byte.
func elementSize(m proto.Message) int {
	n := proto.Size(m)
	return 1 + protowire.SizeVarint(uint64(n)) + n
}

// historyPage builds one page of a run's history, inside a Cut so that every row it reads is that of one
// sequence. The whole page is bounded by the page budget (a message beyond the first that does not fit
// goes to the next page), and what the first page cannot hold of the agents, turns and check-ins it
// counts. Those three may take half the budget together, in that order of preference, so that the
// messages that share the first page always have the other half.
func (s *Service) historyPage(state core.State, run core.RunID, cursor uint64) (*pb.GetRunHistoryResponse, error) {
	resp := &pb.GetRunHistoryResponse{}
	room := s.HistoryPageBytes - pageOverhead
	if cursor == 0 {
		half := room / 2
		fixed := half
		fits := func(n int) bool {
			if n > fixed {
				return false
			}
			fixed -= n
			return true
		}
		agents := state.Agents(run)
		for _, a := range agents {
			msg := wire.AgentMessage(a)
			if !fits(elementSize(msg)) {
				break
			}
			resp.Agents = append(resp.Agents, msg)
		}
		resp.AgentsLeftOutCount = uint32(len(agents) - len(resp.Agents))
		window, err := s.e.Store.Turns(run, time.Time{}, endOfTime, store.MaxWindowTurns)
		if err != nil {
			return nil, fmt.Errorf("read the turns of %s: %w", run, err)
		}
		for _, t := range window.Turns {
			msg := wire.TurnMessage(t)
			if !fits(elementSize(msg)) {
				break
			}
			resp.Turns = append(resp.Turns, msg)
		}
		resp.TurnsLeftOutCount = uint32(window.LeftOut + len(window.Turns) - len(resp.Turns))
		resp.TurnsLeftOut = resp.TurnsLeftOutCount > 0
		all := state.CheckIns(run)
		newest, _ := newestCheckIns(all)
		// Of the check-ins to keep, the newest are the ones to hold, so the cut is of the oldest.
		for _, c := range slices.Backward(newest) {
			msg := wire.CheckInMessage(c)
			if !fits(elementSize(msg)) {
				break
			}
			resp.CheckIns = append(resp.CheckIns, msg)
		}
		slices.Reverse(resp.CheckIns)
		resp.CheckInsLeftOutCount = uint32(len(all) - len(resp.CheckIns))
		resp.CheckInsLeftOut = resp.CheckInsLeftOutCount > 0
		room -= half - fixed
	}
	size := 0
	after := cursor
	for {
		page, err := s.e.Store.Messages(run, after, store.MaxPageMessages)
		if err != nil {
			return nil, fmt.Errorf("read the messages of %s: %w", run, err)
		}
		for _, rec := range page {
			msg := wire.MailboxMessage(rec.Message)
			n := elementSize(msg)
			if len(resp.Messages) > 0 && size+n > room {
				return resp, nil
			}
			size += n
			resp.Messages = append(resp.Messages, msg)
			resp.NextCursor = rec.Seq
		}
		if len(page) < store.MaxPageMessages {
			// That was the last message of the run: there is no next page.
			resp.NextCursor = 0
			return resp, nil
		}
		after = page[len(page)-1].Seq
	}
}
