package runlog_test

import (
	"bytes"
	"compress/gzip"
	"encoding/base64"
	"encoding/json"
	"os"
	"testing"

	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
)

// Raw records, rather than Python-folded keyframes, cross the differential boundary.
// Every generated shape covers ended, running and operator-held prefixes; every
// retained writer is included, so protobuf map iteration cannot hide ordering drift.
func TestRawLogFoldMatchesPythonHead(t *testing.T) {
	file, err := os.Open("../../../proto/testdata/fold/cases.json.gz")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	reader, err := gzip.NewReader(file)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	var cases []struct{ Name, Log, Head string }
	if err := json.NewDecoder(reader).Decode(&cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			raw, err := base64.StdEncoding.DecodeString(c.Log)
			if err != nil {
				t.Fatal(err)
			}
			projection := runlog.NewProjection()
			if err := runlog.ReadPrefix(bytes.NewReader(raw), nil, projection.Record); err != nil {
				t.Fatal(err)
			}
			got, err := runlog.ProjectHead(projection.State)
			if err != nil {
				t.Fatal(err)
			}
			expected, err := base64.StdEncoding.DecodeString(c.Head)
			if err != nil {
				t.Fatal(err)
			}
			// Deterministic protobuf serialization canonicalizes map entries only;
			// repeated fields retain their exact producer order.
			encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(got)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(encoded, expected) {
				t.Fatalf("fold bytes differ from Python for %s", c.Name)
			}
		})
	}
}
