package clidaemon

import (
	"buf.build/go/protovalidate"
	"fmt"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/protobuf/proto"
	"sort"
)

// Token names only the last atomically committed CaughtUp boundary.
type Token struct {
	Epoch    string
	Sequence uint64
}

// Session is a single task's pure stream fold. Its zero value is ready to use.
// Call Disconnected before feeding a new stream. Returned values never alias it.
type Session struct {
	held     map[string]*pb.TaskFact
	pending  map[string]*pb.TaskFact
	token    Token
	epoch    string
	sequence uint64
}

func (s *Session) Token() Token { return s.token }
func (s *Session) Facts() []*pb.TaskFact {
	keys := make([]string, 0, len(s.held))
	for k := range s.held {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]*pb.TaskFact, 0, len(keys))
	for _, k := range keys {
		out = append(out, proto.Clone(s.held[k]).(*pb.TaskFact))
	}
	return out
}

// Disconnected drops an incomplete reset, preserving the readable view and token.
func (s *Session) Disconnected() { s.pending = nil; s.epoch = ""; s.sequence = 0 }

// FactKey identifies the replacement slot of a typed fact.
func FactKey(f *pb.TaskFact) (string, error) {
	switch b := f.GetFact().(type) {
	case *pb.TaskFact_Job:
		if b.Job.GetId() != "" {
			return "job/" + b.Job.GetId(), nil
		}
	case *pb.TaskFact_Result:
		if b.Result.GetJobId() != "" {
			return "result/" + b.Result.GetJobId(), nil
		}
	case *pb.TaskFact_Judgment:
		if b.Judgment.GetCommit() != "" {
			return "judgment/" + b.Judgment.GetCommit(), nil
		}
	case *pb.TaskFact_Promotion:
		if b.Promotion.GetSeq() > 0 {
			return fmt.Sprint("promotion/", b.Promotion.GetSeq()), nil
		}
	case *pb.TaskFact_Plan:
		if b.Plan.GetNumber() > 0 {
			return fmt.Sprint("plan/", b.Plan.GetNumber()), nil
		}
	case *pb.TaskFact_Head:
		if b.Head != nil && b.Head.GetState() != nil {
			return "head", nil
		}
	case *pb.TaskFact_Health:
		if b.Health != nil && b.Health.GetState() != nil {
			return "health", nil
		}
	case *pb.TaskFact_Trouble:
		if b.Trouble.GetJobId() != "" {
			return "trouble/" + b.Trouble.GetJobId(), nil
		}
	}
	return "", fmt.Errorf("invalid task fact %v", f)
}

// Apply rejects gaps and malformed batches before changing any state. A diff is
// atomic too. Only CaughtUp advances the resume token, even after live diffs.
func (s *Session) Apply(m *pb.WatchTaskResponse) (bool, error) {
	if m == nil {
		return false, &Unreadable{StreamLost, "missing WatchTask response"}
	}
	if err := protovalidate.Validate(m); err != nil {
		return false, &Unreadable{StreamLost, err.Error()}
	}
	bad := func() (bool, error) { return false, &Unreadable{StreamLost, "invalid WatchTask ordering or fact"} }
	if m.GetEpoch() == "" || m.GetSequence() == 0 {
		return bad()
	}
	if s.epoch == "" {
		// Protocol 3 always resets, even when the request carries a resume token.
		// Accepting replayed deltas here could mix them with changes newer than
		// the last CaughtUp token still held in the readable view.
		if m.GetReset_() == nil || m.GetSequence() != 1 {
			return bad()
		}
	} else if m.GetEpoch() != s.epoch || m.GetSequence() != s.sequence+1 {
		return bad()
	}
	changed := false
	switch b := m.GetBody().(type) {
	case *pb.WatchTaskResponse_Reset_:
		if b.Reset_ == nil || s.pending != nil {
			return bad()
		}
		s.pending = map[string]*pb.TaskFact{}
	case *pb.WatchTaskResponse_Facts:
		if len(b.Facts.GetFacts()) == 0 || len(b.Facts.GetFacts()) > 500 {
			return bad()
		}
		batch := map[string]*pb.TaskFact{}
		for _, f := range b.Facts.GetFacts() {
			k, err := FactKey(f)
			if err != nil {
				return bad()
			}
			batch[k] = proto.Clone(f).(*pb.TaskFact)
		}
		into := s.pending
		if into == nil {
			if s.held == nil {
				return bad()
			}
			into = s.held
			changed = true
		}
		for k, f := range batch {
			into[k] = f
		}
	case *pb.WatchTaskResponse_CaughtUp:
		if b.CaughtUp == nil || s.pending == nil {
			return bad()
		}
		s.held, s.pending = s.pending, nil
		s.token = Token{m.Epoch, m.Sequence}
		changed = true
	default:
		return bad()
	}
	s.epoch, s.sequence = m.Epoch, m.Sequence
	return changed, nil
}
