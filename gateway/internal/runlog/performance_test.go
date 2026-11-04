package runlog_test

import (
	"compress/gzip"
	"encoding/base64"
	"encoding/json"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
	"io"
	"os"
	"testing"
)

// The histories include failed retries, pending ideas, noise, worse readings,
// both directions, zero input and unattributed gates. Expectations are the core's
// independently property-tested decisions, not a second Go best calculation.
func TestRetainedPerformanceMatchesCoreForGeneratedHistories(t *testing.T) {
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
	data, err := io.ReadAll(reader)
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name     string `json:"name"`
		Skeleton string `json:"skeleton"`
		Head     string `json:"head"`
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			raw, err := base64.StdEncoding.DecodeString(c.Skeleton)
			if err != nil {
				t.Fatal(err)
			}
			state := &storev1.Keyframe{}
			if err := proto.Unmarshal(raw, state); err != nil {
				t.Fatal(err)
			}
			raw, err = base64.StdEncoding.DecodeString(c.Head)
			if err != nil {
				t.Fatal(err)
			}
			want := &storev1.RunHead{}
			if err := proto.Unmarshal(raw, want); err != nil {
				t.Fatal(err)
			}
			got, err := runlog.ProjectHead(state)
			if err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(got, want) {
				t.Fatalf("Go retained projection differs from core\ngot: %v\nwant: %v", got, want)
			}
		})
	}
}
