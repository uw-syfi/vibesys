package server_test

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"
)

func expandCorpus(value any) any {
	switch v := value.(type) {
	case []any:
		for i, item := range v {
			v[i] = expandCorpus(item)
		}
		return v
	case map[string]any:
		if count, ok := v["$bytes"].(float64); ok {
			return base64.StdEncoding.EncodeToString(make([]byte, int(count)))
		}
		if count, ok := v["$string"].(float64); ok {
			return strings.Repeat("x", int(count))
		}
		if count, ok := v["$repeat"].(float64); ok {
			items := make([]any, int(count))
			for i := range items {
				items[i] = expandCorpus(v["value"])
			}
			return items
		}
		for key, item := range v {
			v[key] = expandCorpus(item)
		}
		return v
	default:
		return value
	}
}

func TestHostValidationCorpus(t *testing.T) {
	data, err := os.ReadFile("../../../proto/testdata/host-validation.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name, Message, Field string
		Value                any
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, item := range cases {
		t.Run(item.Name, func(t *testing.T) {
			kind, err := protoregistry.GlobalTypes.FindMessageByName(protoreflect.FullName("vibesys.api.v1." + item.Message))
			if err != nil {
				t.Fatal(err)
			}
			message := kind.New().Interface()
			encoded, err := json.Marshal(expandCorpus(item.Value))
			if err != nil {
				t.Fatal(err)
			}
			if err := protojson.Unmarshal(encoded, message); err != nil {
				t.Fatal(err)
			}
			err = protovalidate.Validate(message)
			if item.Field == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), item.Field) {
				t.Fatalf("expected %s violation, got %v", item.Field, err)
			}
		})
	}
}

func TestHostRejectsSchemaInvalidRequests(t *testing.T) {
	f := start(t)
	c := client(t, f, false)
	tests := []struct {
		name, field string
		call        func() error
	}{
		{"Hello", "api_schema_hash", func() error { _, err := c.Hello(f.ctx, &apiv1.HelloRequest{}); return err }},
		{"Skeleton", "run_id", func() error { _, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{}); return err }},
		{"GetBlobs", "sha256", func() error {
			_, err := c.GetBlobs(f.ctx, &apiv1.GetBlobsRequest{RunId: "run-1", Sha256: []string{"../escape"}})
			return err
		}},
		{"Tail", "session_id", func() error {
			stream, err := c.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1", SessionId: "../escape"})
			if err != nil {
				return err
			}
			_, err = stream.Recv()
			return err
		}},
		{"TailStream", "stream_id", func() error {
			stream, err := c.TailStream(f.ctx, &apiv1.TailStreamRequest{RunId: "run-1"})
			if err != nil {
				return err
			}
			_, err = stream.Recv()
			return err
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := test.call()
			if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), test.field) {
				t.Fatalf("expected InvalidArgument for %s, got %v", test.field, err)
			}
		})
	}
}

// An invalid head is never streamed. It is the damage of its own run: the run is listed by name
// without a head and the listing completes.
func TestHostWithholdsInvalidHeadFromTheListing(t *testing.T) {
	f := start(t)
	value := head(nil)
	value.Strategy = ""
	publishHead(t, f, value)
	stream, err := watchRuns(f.ctx, client(t, f, false))
	if err != nil {
		t.Fatal(err)
	}
	entry, err := stream.Recv()
	if err != nil || entry.GetRunId() != "run-1" || entry.GetHead() != nil {
		t.Fatalf("expected run-1 listed without its invalid head, got %v, %v", entry, err)
	}
	if marker, err := stream.Recv(); err != nil || !marker.GetInitialComplete() {
		t.Fatalf("expected the listing to complete, got %v, %v", marker, err)
	}
}

func TestHostCallValidationKeepsItsFailureEnvelope(t *testing.T) {
	runner := hostcommand.FakeRunner{Program: func(_ context.Context, _ hostcommand.Command) (hostcommand.Result, error) {
		output, err := proto.Marshal(&apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_ConfigurationFailed{ConfigurationFailed: &apiv1.ConfigurationFailed{Stage: "test", Message: "missing code"}}})
		return hostcommand.Result{Output: output}, err
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).HostCall(f.ctx, intentRequest())
	failure := response.GetConfigurationFailed()
	if err != nil || failure.GetCode() != "invalid_host_response" || !strings.Contains(failure.GetMessage(), "configuration_failed.code") {
		t.Fatalf("expected typed invalid configuration_failed.code, got %v %v", response, err)
	}
}

func TestHostRejectsAnUnrepresentableSkeletonContinuation(t *testing.T) {
	f := start(t)
	seq := ^uint64(0)
	publishHead(t, f, head(&seq))
	data := append([]byte(runlog.Magic), frame(payload(t, seq, "record"))...)
	write(t, filepath.Join(f.run, "run.log"), false, data)
	_, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if status.Code(err) != codes.Internal || !strings.Contains(err.Error(), "committed_seq") {
		t.Fatalf("expected invalid unary committed_seq, got %v", err)
	}
}
