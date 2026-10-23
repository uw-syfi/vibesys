package runlog_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"testing"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
)

// retainedCases are the retained corpus cases in manifest order: each previous writer's log,
// the sequence of its input measurement and the span that measurement is bound to.
func retainedCases(tb testing.TB) (logs [][]byte, measurementSeq []uint64, spans []string) {
	tb.Helper()
	root := "../../../proto/testdata/retained"
	manifest, err := os.ReadFile(filepath.Join(root, "manifest.json"))
	if err != nil {
		tb.Fatal(err)
	}
	var cases map[string]struct {
		MeasurementSeq uint64 `json:"measurement_seq"`
		InputSpan      string `json:"input_span"`
	}
	if err := json.Unmarshal(manifest, &cases); err != nil {
		tb.Fatal(err)
	}
	var names []string
	for name := range cases {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		log, err := os.ReadFile(filepath.Join(root, name, "run.log"))
		if err != nil {
			tb.Fatal(err)
		}
		logs = append(logs, log)
		measurementSeq = append(measurementSeq, cases[name].MeasurementSeq)
		spans = append(spans, cases[name].InputSpan)
	}
	return logs, measurementSeq, spans
}

// Every numeric input, both previous encodings and explicit unmeasured facts, for every
// retained case. The seed corpus pins the reported gate-bound measurement and its spanless sibling.
func FuzzInputUpgradePreservesFactsAndBodies(f *testing.F) {
	logs, measurementSeq, spans := retainedCases(f)
	for which := range logs {
		f.Add(uint8(which), int64(100), false, false)
		f.Add(uint8(which), int64(-17), false, false)
		f.Add(uint8(which), int64(0), true, false)
		f.Add(uint8(which), int64(100), false, true)
	}
	f.Fuzz(func(t *testing.T, which uint8, value int64, spanless, explicit bool) {
		source, measured, span := logs[int(which)%len(logs)], measurementSeq[int(which)%len(logs)], spans[int(which)%len(logs)]
		reader := runlog.NewReader(bytes.NewReader(source))
		upgrade := runlog.NewInputUpgrade(nil)
		seen := false
		for seq := uint64(1); ; seq++ {
			payload, err := reader.Next()
			if errors.Is(err, io.EOF) {
				break
			}
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
			if point := original.GetMetric(); point != nil && point.GetSpan() == span {
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
				if explicit || seq != measured || fact.GetMeasured().GetValue() != float64(value) {
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
