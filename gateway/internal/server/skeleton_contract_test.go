package server_test

import (
	"encoding/json"
	"fmt"
	"os"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// The shared Skeleton contract: the same cases drive this service and the
// client's simulated gateway (clients/libs/run-skeleton sim-gateway), so the
// simulation cannot be more capable or more forgiving than the service.
type skeletonContract struct {
	Constants struct {
		KeyframeCandidates int `json:"keyframeCandidates"`
		ReplyBytes         int `json:"replyBytes"`
		MaxStoredKeyframe  int `json:"maxStoredKeyframe"`
	} `json:"constants"`
	Selection []struct {
		Name        string `json:"name"`
		Checkpoints []struct {
			Through uint64 `json:"through"`
			Fits    bool   `json:"fits"`
			Stored  string `json:"stored"`
		} `json:"checkpoints"`
		Chosen uint64 `json:"chosen"`
	} `json:"selection"`
	Logs []struct {
		Name     string `json:"name"`
		Log      string `json:"log"`
		Skeleton struct {
			Status       int    `json:"status"`
			Complete     bool   `json:"complete"`
			CommittedSeq uint64 `json:"committedSeq"`
			NextSeq      uint64 `json:"nextSeq"`
			Records      int    `json:"records"`
			Keyframe     bool   `json:"keyframe"`
		} `json:"skeleton"`
	} `json:"logs"`
}

func loadContract(t *testing.T) skeletonContract {
	t.Helper()
	data, err := os.ReadFile("../../../proto/testdata/skeleton-contract/cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var contract skeletonContract
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatal(err)
	}
	return contract
}

func TestSkeletonKeyframeSelectionContract(t *testing.T) {
	contract := loadContract(t)
	if contract.Constants.ReplyBytes != 1<<20 {
		t.Fatalf("the contract's reply budget is %d, the service's is %d", contract.Constants.ReplyBytes, 1<<20)
	}
	for _, c := range contract.Selection {
		t.Run(c.Name, func(t *testing.T) {
			f := start(t)
			var last uint64
			keyframes := map[uint64][]*storev1.EpochVersion{}
			for _, point := range c.Checkpoints {
				size := 1 << 10
				if !point.Fits {
					size = contract.Constants.ReplyBytes + 1<<10
				}
				set := descriptorSet(fmt.Sprint("set", point.Through), size)
				epochs := []*storev1.EpochVersion{epochWith(point.Through, set)}
				if point.Stored == "over" {
					// Many epochs share one set: small addressed, over the read limit stored.
					size = 600 << 10
					set = descriptorSet(fmt.Sprint("set", point.Through), size)
					epochs = nil
					for epoch := 0; (epoch+1)*size <= contract.Constants.MaxStoredKeyframe+size; epoch++ {
						epochs = append(epochs, epochWith(uint64(epoch)+1, set))
					}
				}
				keyframes[point.Through] = epochs
				last = max(last, point.Through)
			}
			storeRun(t, f, int(max(last, 6)), keyframes)
			got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
			if err != nil {
				t.Fatal(err)
			}
			var through uint64
			if len(got.Keyframe) > 0 {
				through = restored(t, got, nil).ThroughSeq
			}
			if through != c.Chosen {
				t.Fatalf("chose the keyframe through %d, the contract says %d", through, c.Chosen)
			}
		})
	}
}

func TestSkeletonLogContract(t *testing.T) {
	for _, c := range loadContract(t).Logs {
		t.Run(c.Name, func(t *testing.T) {
			f := start(t)
			run := "run-1"
			if c.Log == "unindexed" {
				run = "run-unknown"
			}
			got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: run})
			if c.Skeleton.Status != 0 {
				if status.Code(err) != codes.Code(c.Skeleton.Status) {
					t.Fatalf("status %v, the contract says %d", status.Code(err), c.Skeleton.Status)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			want := c.Skeleton
			if got.Complete != want.Complete || got.CommittedSeq != want.CommittedSeq || got.NextSeq != want.NextSeq || len(got.Records) != want.Records || (len(got.Keyframe) > 0) != want.Keyframe {
				t.Fatalf("complete=%v committed=%d next=%d records=%d keyframe=%d", got.Complete, got.CommittedSeq, got.NextSeq, len(got.Records), len(got.Keyframe))
			}
		})
	}
}
