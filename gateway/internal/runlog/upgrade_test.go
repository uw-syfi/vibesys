package runlog_test

import (
	"os"
	"testing"

	"bytes"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
)

// Every numeric input, both previous encodings and explicit unmeasured facts.
// The seed corpus pins the reported gate-bound measurement and its spanless sibling.
func FuzzInputUpgradePreservesFactsAndBodies(f *testing.F) {
	f.Add(int64(100), false, false)
	f.Add(int64(-17), false, false)
	f.Add(int64(0), true, false)
	f.Add(int64(100), false, true)
	source, err := os.ReadFile("../../../proto/testdata/retained/64c1a8b7/run.log")
	if err != nil {
		f.Fatal(err)
	}
	f.Fuzz(func(t *testing.T, value int64, spanless, explicit bool) {
		reader := runlog.NewReader(bytes.NewReader(source))
		upgrade := runlog.NewInputUpgrade(nil)
		seen := false
		for seq := uint64(1); seq <= 18; seq++ {
			payload, err := reader.Next()
			if err != nil {
				t.Fatal(err)
			}
			original := &storev1.Record{}
			if err := proto.Unmarshal(payload, original); err != nil {
				t.Fatal(err)
			}
			if epoch := original.GetEpochStarted(); epoch.GetRun() != nil && explicit {
				epoch.Run.InputMeasurement = &storev1.InputMeasurement{State: &storev1.InputMeasurement_Unmeasured{Unmeasured: &storev1.UnmeasuredInput{}}}
			}
			if point := original.GetMetric(); point != nil && point.GetSpan() == "input-gate" {
				point.Value = float64(value)
				if spanless {
					point.Span = nil
				}
			}
			payload, err = proto.Marshal(original)
			if err != nil {
				t.Fatal(err)
			}
			normalized, err := upgrade.Record(payload)
			if err != nil {
				t.Fatal(err)
			}
			got := &storev1.Record{}
			if err := proto.Unmarshal(normalized, got); err != nil {
				t.Fatal(err)
			}
			fact := got.InputMeasurement
			got.InputMeasurement = nil
			if !proto.Equal(got, original) {
				t.Fatalf("original body or sequence changed at %d", seq)
			}
			if fact != nil {
				seen = true
				if explicit || seq != 14 || fact.GetMeasured().GetValue() != float64(value) {
					t.Fatalf("unexpected input fact at %d: %v", seq, fact)
				}
				if fact.GetMeasured().Span != original.GetMetric().GetSpan() {
					t.Fatal("invented an input span")
				}
			}
		}
		if seen == explicit {
			t.Fatal("input fact presence disagrees with the recorded declaration")
		}
	})
}
