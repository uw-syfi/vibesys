package server_test

import (
	"os"
	"path/filepath"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Descriptor addressing must preserve every additive semantic fact, including positions
// farther back than a client's raw-record window. The old flag must remain distinguishable.
func TestSkeletonPreservesCheckpointRecordFacts(t *testing.T) {
	for _, tracked := range []bool{false, true} {
		t.Run(map[bool]string{false: "previous", true: "current"}[tracked], func(t *testing.T) {
			f := start(t)
			epochs := []*storev1.EpochVersion{epochWith(1, descriptorSet("shared", 1000))}
			storeRun(t, f, 1400, map[uint64][]*storev1.EpochVersion{1400: epochs})
			path := filepath.Join(f.run, "keyframes", "1400.pb")
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			frame := &storev1.Keyframe{}
			if err := proto.Unmarshal(data, frame); err != nil {
				t.Fatal(err)
			}
			if tracked {
				frame.RecordsTracked = true
				frame.Epochs[0].StartsEpoch = true
				frame.Epochs[0].StartedAt = &timestamppb.Timestamp{Seconds: 1}
				for action := storev1.IntentAction_INTENT_ACTION_STEER; action <= storev1.IntentAction_INTENT_ACTION_PAUSE_NOW; action++ {
					seq := uint64(action + 1)
					step := storev1.IntentStep_INTENT_STEP_APPLIED
					intent := &storev1.IntentState{ControlId: action.String(), Action: action, Step: step}
					if seq%2 == 0 {
						intent.Step = storev1.IntentStep_INTENT_STEP_REFUSED
						intent.Reason = proto.String("epoch closed")
					}
					frame.Intents = append(frame.Intents, &storev1.Record{Seq: seq, Epoch: 1, At: &timestamppb.Timestamp{Seconds: int64(seq)}, Body: &storev1.Record_Intent{Intent: intent}})
				}
			}
			write(t, path, false, encode(t, frame))
			reply, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
			if err != nil {
				t.Fatal(err)
			}
			actual := restored(t, reply, nil)
			if !proto.Equal(actual, frame) || actual.RecordsTracked != tracked {
				t.Fatalf("checkpoint facts changed across Skeleton: %v", actual)
			}
		})
	}
}
