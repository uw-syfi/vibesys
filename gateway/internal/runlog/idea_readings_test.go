package runlog_test

import (
	"compress/gzip"
	"encoding/base64"
	"encoding/json"
	"fmt"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
	"os"
	"testing"
)

// Use the same generated histories and trailing-check counts as Python and TypeScript.
func TestUnmeasuredChecksKeepEveryCoreIdeaReading(t *testing.T) {
	f, err := os.Open("../../../proto/testdata/performance/cases.json.gz")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	reader, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	var cases []struct {
		Name     string
		Skeleton string
	}
	if err := json.NewDecoder(reader).Decode(&cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			for checks := 1; checks <= 3; checks++ {
				data, err := base64.StdEncoding.DecodeString(c.Skeleton)
				if err != nil {
					t.Fatal(err)
				}
				state := &storev1.Keyframe{}
				if err := proto.Unmarshal(data, state); err != nil {
					t.Fatal(err)
				}
				before, err := runlog.ProjectHead(state)
				if err != nil {
					t.Fatal(err)
				}
				var gates []*storev1.Span
				for _, h := range state.Spans {
					s := h.Versions[len(h.Versions)-1].Span
					if s.GetLabel().GetKind() == "gate" {
						gates = append(gates, s)
					}
				}
				for _, original := range gates {
					for at := 0; at < checks; at++ {
						s := proto.Clone(original).(*storev1.Span)
						s.Id = fmt.Sprintf("%s-check-%d", s.Id, at)
						s.StartedAt += float64(1000 + at)
						state.ThroughSeq++
						state.Spans[s.Id] = &storev1.SpanHistory{Versions: []*storev1.SpanVersion{{Seq: state.ThroughSeq, Span: s}}}
					}
				}
				after, err := runlog.ProjectHead(state)
				if err != nil {
					t.Fatal(err)
				}
				for i, series := range after.Performance {
					if !proto.Equal(series, before.Performance[i]) {
						t.Fatal("an unmeasured check erased a core reading")
					}
				}
			}
		})
	}
}
