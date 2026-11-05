package plan_test

import (
	"bytes"
	"testing"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/messages"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
)

// BenchmarkRead reads a plan of yardstick-sized operations (about 200 bytes each).
func BenchmarkRead(b *testing.B) {
	var ops []*pb.PlannedOperation
	for i := range 10_000 {
		step := messages.Query(messages.Query(messages.Query(messages.Get("/hotels"), "inDate", "2015-04-09"), "outDate", "2015-04-11"), "lat", "37.7867")
		messages.Header(step, "X-Request", "r-", string(rune('a'+i%26)))
		ops = append(ops, messages.Exclusive(messages.Op(0, step), "k"+string(rune('a'+i%26))))
	}
	data := messages.RequestsFile(files.Binpb, []string{"search"}, ops...)
	b.SetBytes(int64(len(data)))
	b.ResetTimer()
	for range b.N {
		if _, err := plan.Read(bytes.NewReader(data), files.Binpb); err != nil {
			b.Fatal(err)
		}
	}
}
