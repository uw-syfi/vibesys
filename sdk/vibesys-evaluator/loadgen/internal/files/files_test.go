package files_test

import (
	"bytes"
	"errors"
	"io"
	"math/rand/v2"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
)

func randomResult(random *rand.Rand) *pb.OperationResult {
	result := &pb.OperationResult{
		Index:              random.Uint64(),
		Operation:          random.Uint32N(8),
		ScheduledUnixNanos: random.Int64(),
		StartUnixNanos:     random.Int64(),
		EndUnixNanos:       random.Int64(),
		Error:              pb.ErrorKind(random.IntN(6)),
	}
	for range random.IntN(4) {
		step := &pb.StepResult{Status: uint32(random.IntN(600)), ErrorMessage: "line\nbreak \"quoted\""}
		if random.IntN(2) == 0 {
			body := make([]byte, random.IntN(64))
			for i := range body {
				body[i] = byte(random.IntN(256))
			}
			step.Response = &pb.StepResult_Body{Body: body}
		} else {
			step.Response = &pb.StepResult_Digest{Digest: &pb.BodyDigest{Sha256: make([]byte, 32), Length: random.Uint64()}}
		}
		result.Steps = append(result.Steps, step)
	}
	return result
}

// Property: every message written in either format reads back equal, in order, then io.EOF.
func TestRoundTripPreservesMessages(t *testing.T) {
	for _, format := range []files.Format{files.Binpb, files.JSON} {
		for seed := range uint64(50) {
			random := rand.New(rand.NewPCG(seed, 1))
			var messages []*pb.OperationResult
			for range random.IntN(20) {
				messages = append(messages, randomResult(random))
			}
			var buf bytes.Buffer
			writer, err := files.NewWriter(&buf, format, files.Header(pb.FileKind_FILE_KIND_RESULTS, "test"))
			if err != nil {
				t.Fatal(err)
			}
			for _, message := range messages {
				if err := writer.Write(message); err != nil {
					t.Fatal(err)
				}
			}
			if err := writer.Flush(); err != nil {
				t.Fatal(err)
			}
			if format == files.JSON && bytes.Count(buf.Bytes(), []byte("\n")) != len(messages)+1 {
				t.Fatalf("JSON lines: want %d lines", len(messages)+1)
			}
			reader, header, err := files.NewReader(&buf, format, pb.FileKind_FILE_KIND_RESULTS)
			if err != nil {
				t.Fatal(err)
			}
			if header.GetWriter() != "test" {
				t.Fatalf("header %v", header)
			}
			for i, want := range messages {
				got := &pb.OperationResult{}
				if err := reader.Read(got); err != nil {
					t.Fatalf("format %d seed %d message %d: %v", format, seed, i, err)
				}
				if !proto.Equal(got, want) {
					t.Fatalf("message %d: got %v want %v", i, got, want)
				}
			}
			if err := reader.Read(&pb.OperationResult{}); !errors.Is(err, io.EOF) {
				t.Fatalf("want EOF, got %v", err)
			}
		}
	}
}

func TestReaderRejectsOtherSchemaVersionsAndKinds(t *testing.T) {
	for _, format := range []files.Format{files.Binpb, files.JSON} {
		for _, version := range []uint32{0, 2, 7} {
			var buf bytes.Buffer
			header := files.Header(pb.FileKind_FILE_KIND_REQUESTS, "test")
			header.SchemaVersion = version
			writer, _ := files.NewWriter(&buf, format, header)
			_ = writer.Flush()
			if _, _, err := files.NewReader(&buf, format, pb.FileKind_FILE_KIND_REQUESTS); !errors.Is(err, files.ErrSchemaVersion) {
				t.Fatalf("version %d: want ErrSchemaVersion, got %v", version, err)
			}
		}
		var buf bytes.Buffer
		writer, _ := files.NewWriter(&buf, format, files.Header(pb.FileKind_FILE_KIND_SUMMARY, "test"))
		_ = writer.Flush()
		if _, _, err := files.NewReader(&buf, format, pb.FileKind_FILE_KIND_REQUESTS); !errors.Is(err, files.ErrMalformed) {
			t.Fatalf("kind mismatch: want ErrMalformed, got %v", err)
		}
		if _, _, err := files.NewReader(bytes.NewReader(nil), format, pb.FileKind_FILE_KIND_REQUESTS); !errors.Is(err, files.ErrMalformed) {
			t.Fatalf("empty: want ErrMalformed, got %v", err)
		}
	}
}

func TestJSONReaderRejectsUnknownFields(t *testing.T) {
	input := `{"schemaVersion":1,"kind":"FILE_KIND_RESULTS","writer":"x","surprise":1}` + "\n"
	if _, _, err := files.NewReader(bytes.NewBufferString(input), files.JSON, pb.FileKind_FILE_KIND_RESULTS); !errors.Is(err, files.ErrMalformed) {
		t.Fatalf("want ErrMalformed, got %v", err)
	}
}

func TestBinpbReaderRejectsTruncatedMessage(t *testing.T) {
	var buf bytes.Buffer
	writer, _ := files.NewWriter(&buf, files.Binpb, files.Header(pb.FileKind_FILE_KIND_RESULTS, "test"))
	_ = writer.Write(&pb.OperationResult{Index: 5, StartUnixNanos: 99})
	_ = writer.Flush()
	truncated := buf.Bytes()[:buf.Len()-2]
	reader, _, err := files.NewReader(bytes.NewReader(truncated), files.Binpb, pb.FileKind_FILE_KIND_RESULTS)
	if err != nil {
		t.Fatal(err)
	}
	if err := reader.Read(&pb.OperationResult{}); !errors.Is(err, files.ErrMalformed) {
		t.Fatalf("want ErrMalformed, got %v", err)
	}
}
