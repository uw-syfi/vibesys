package wire_test

import (
	"buf.build/go/protovalidate"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"testing"
)

// Generated malformed messages exercise the schema itself, so both languages
// enforce the same shape without copying the validation into handwritten code.
func FuzzTaskMessageValidation(f *testing.F) {
	for i := range 12 {
		f.Add(uint8(i), uint64(i))
	}
	f.Fuzz(func(t *testing.T, variant uint8, n uint64) {
		var message proto.Message
		switch variant % 12 {
		case 0:
			message = &cliv1.TaskFact{}
		case 1:
			message = &cliv1.TaskFacts{}
		case 2:
			message = &cliv1.WatchTaskResponse{Epoch: "E", Sequence: n}
		case 3:
			message = &cliv1.Head{}
		case 4:
			message = &cliv1.StoreHealth{}
		case 5:
			message = &cliv1.Judgment{Commit: "invalid", Against: "invalid"}
		case 6:
			message = &cliv1.PromotionRecord{Seq: n}
		case 7:
			message = &cliv1.Away{Reason: cliv1.AwayReason(n)}
		case 8:
			message = &cliv1.JobTrouble{}
		case 9:
			message = &cliv1.Submitter{}
		case 10:
			message = &cliv1.Submitter{At: &timestamppb.Timestamp{Seconds: 9223372036854775807}}
		case 11:
			facts := make([]*cliv1.TaskFact, 501+int(n%10))
			for i := range facts {
				facts[i] = &cliv1.TaskFact{Fact: &cliv1.TaskFact_Health{Health: &cliv1.StoreHealth{State: &cliv1.StoreHealth_Live{Live: &cliv1.Live{}}}}}
			}
			message = &cliv1.TaskFacts{Facts: facts}
		}
		if err := protovalidate.Validate(message); err == nil {
			t.Fatalf("accepted malformed %T: %v", message, message)
		}
	})
}
