package runlog_test

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/gateway/gen"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type fixtureRecord struct {
	Seq          uint64                 `json:"seq"`
	At           *timestamppb.Timestamp `json:"at"`
	Epoch        uint32                 `json:"epoch"`
	EpochStarted *storev1.EpochStarted  `json:"epoch_started"`
	LegacyEvent  *storev1.LegacyEvent   `json:"legacy_event"`
}

type fixture struct {
	Records []fixtureRecord `json:"records"`
	Stop    string          `json:"stop"`
	Offset  int64           `json:"offset"`
}

func TestSharedPythonFixtures(t *testing.T) {
	root := filepath.Join("..", "..", "..", "proto", "testdata", "runlog")
	manifest, err := os.ReadFile(filepath.Join(root, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixtures map[string]fixture
	decoder := json.NewDecoder(bytes.NewReader(manifest))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&fixtures); err != nil {
		t.Fatal(err)
	}
	files, err := filepath.Glob(filepath.Join(root, "*.runlog"))
	if err != nil {
		t.Fatal(err)
	}
	for _, path := range files {
		if _, ok := fixtures[filepath.Base(path)]; !ok {
			t.Fatalf("fixture %s is missing from manifest", path)
		}
	}
	for name, expected := range fixtures {
		t.Run(name, func(t *testing.T) {
			contents, err := os.ReadFile(filepath.Join(root, name))
			if err != nil {
				t.Fatal(err)
			}
			reader := runlog.NewReader(bytes.NewReader(contents))
			boundary := int64(len(gen.RunLogMagic))
			for _, fields := range expected.Records {
				payload, err := reader.Next()
				if err != nil {
					t.Fatalf("record %d: %v", fields.Seq, err)
				}
				var actual storev1.Record
				if err := proto.Unmarshal(payload, &actual); err != nil {
					t.Fatal(err)
				}
				record := &storev1.Record{Seq: fields.Seq, At: fields.At, Epoch: fields.Epoch}
				switch {
				case fields.EpochStarted != nil && fields.LegacyEvent == nil:
					record.Body = &storev1.Record_EpochStarted{EpochStarted: fields.EpochStarted}
				case fields.LegacyEvent != nil && fields.EpochStarted == nil:
					record.Body = &storev1.Record_LegacyEvent{LegacyEvent: fields.LegacyEvent}
				default:
					t.Fatal("manifest record must have exactly one body")
				}
				if !proto.Equal(&actual, record) {
					t.Fatalf("record %d: got %v, want %v", fields.Seq, &actual, record)
				}
				var prefix [binary.MaxVarintLen64]byte
				boundary += int64(binary.PutUvarint(prefix[:], uint64(len(payload))) + 4 + len(payload))
			}
			_, err = reader.Next()
			switch expected.Stop {
			case "end", "torn_tail":
				// The gateway projects both retryable boundaries to io.EOF.
				if !errors.Is(err, io.EOF) {
					t.Fatalf("got %v, want EOF", err)
				}
				if boundary != expected.Offset {
					t.Fatalf("committed prefix ends at %d, want %d", boundary, expected.Offset)
				}
			case "corruption":
				var corrupt *runlog.Corruption
				if !errors.As(err, &corrupt) {
					t.Fatalf("got %v, want corruption", err)
				}
				if corrupt.Offset != expected.Offset {
					t.Fatalf("corruption offset %d, want %d", corrupt.Offset, expected.Offset)
				}
			default:
				t.Fatalf("unknown manifest stop %q", expected.Stop)
			}
		})
	}
}
