package wire_test

import (
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"testing"
)

func TestSearchStateValidation(t *testing.T) {
	valid := &pb.SearchState{RunId: "r", Revision: 1, Health: &pb.SearchHealth{State: &pb.SearchHealth_LiveSince{LiveSince: &timestamppb.Timestamp{Seconds: 1}}}}
	if err := wire.Validate(valid); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(*pb.SearchState){
		"run":      func(s *pb.SearchState) { s.RunId = "" },
		"revision": func(s *pb.SearchState) { s.Revision = 0 },
		"health":   func(s *pb.SearchState) { s.Health = nil },
		"union":    func(s *pb.SearchState) { s.Health = &pb.SearchHealth{} },
		"unspecified": func(s *pb.SearchState) {
			s.Health.State = &pb.SearchHealth_Unreadable{Unreadable: &pb.Unreadable{Since: &timestamppb.Timestamp{}}}
		},
		"unknown": func(s *pb.SearchState) {
			s.Health.State = &pb.SearchHealth_Unreadable{Unreadable: &pb.Unreadable{Reason: 99, Since: &timestamppb.Timestamp{}}}
		},
		"since": func(s *pb.SearchState) {
			s.Health.State = &pb.SearchHealth_Unreadable{Unreadable: &pb.Unreadable{Reason: pb.UnreadableReason_UNREADABLE_REASON_NO_DAEMON}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			s := proto.Clone(valid).(*pb.SearchState)
			mutate(s)
			if wire.Validate(s) == nil {
				t.Fatal("accepted invalid search state")
			}
		})
	}
	for reason := pb.UnreadableReason_UNREADABLE_REASON_NO_DAEMON; reason <= pb.UnreadableReason_UNREADABLE_REASON_STREAM_LOST; reason++ {
		s := proto.Clone(valid).(*pb.SearchState)
		s.Health.State = &pb.SearchHealth_Unreadable{Unreadable: &pb.Unreadable{Reason: reason, Since: &timestamppb.Timestamp{}}}
		if err := wire.Validate(s); err != nil {
			t.Fatal(err)
		}
	}
}
