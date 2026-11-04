package server_test

import (
	"fmt"
	"strings"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

// Vary the observed head beside a checkpoint that fits on its own. Selection
// must account for both and fall back to replay whenever they do not fit together.
func TestSkeletonBudgetIncludesObservedHead(t *testing.T) {
	for _, entries := range []int{0, 4, 8, 16, 24} {
		t.Run(fmt.Sprint(entries), func(t *testing.T) {
			f := start(t)
			epochs := []*storev1.EpochVersion{epochWith(1, descriptorSet("large", 900<<10))}
			storeRun(t, f, 6, map[uint64][]*storev1.EpochVersion{6: epochs})
			observed := head(proto.Uint64(6))
			observed.Epoch, observed.PerformanceStated = 1, true
			observed.SpanCounts = map[string]uint64{}
			for index := 0; index < entries; index++ {
				observed.SpanCounts[fmt.Sprint(index)+strings.Repeat("a", 32<<10)] = 1
			}
			publishHead(t, f, observed)
			reply, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
			if err != nil {
				t.Fatal(err)
			}
			if proto.Size(reply) > 1<<20 {
				t.Fatalf("reply with %d head entries exceeds budget: %d", entries, proto.Size(reply))
			}
			if !proto.Equal(reply.Head, observed) || !reply.Complete || reply.CommittedSeq != 6 {
				t.Fatal("budget selection lost committed head or replay facts")
			}
		})
	}
}
