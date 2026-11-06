package vseval

import (
	"bytes"
	"encoding/json"
	"fmt"

	"google.golang.org/protobuf/encoding/protojson"

	evaluatorv1 "github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval/internal/gen/vibesys/evaluator/v1"
)

// Record kinds defined by the protocol.
const (
	kindHello  = "hello"
	kindResult = "result"
	kindError  = "error"
)

// metricSpec is the declared metadata of one metric. Every field is optional,
// so a metric declared without options is written as {}. Required is a pointer
// because its protocol default is true: only an optional metric puts the key
// on the wire.
type metricSpec struct {
	Unit      string
	Direction Dir
	Required  *bool
}

// required reports whether every successful run must carry this metric.
func (s metricSpec) required() bool { return s.Required == nil || *s.Required }

// record returns the definition's message for the spec, leaving unset what the
// evaluator did not set so that it is not written.
func (s metricSpec) record() *evaluatorv1.MetricSpec {
	out := &evaluatorv1.MetricSpec{Required: s.Required}
	if s.Unit != "" {
		out.Unit = &s.Unit
	}
	if s.Direction != "" {
		direction := string(s.Direction)
		out.Direction = &direction
	}
	return out
}

// encode returns the one-line JSON of a record, with the proto field names the
// protocol's readers expect. The records are the definition's generated
// messages (proto/vibesys/evaluator/v1/records.proto), so what is written is
// what the definition says and not a second description of it.
func encode(rec *evaluatorv1.ResultRecord) ([]byte, error) {
	raw, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(rec)
	if err != nil {
		return nil, fmt.Errorf("encode %s record: %w", rec.GetKind(), err)
	}
	// protojson varies its spacing from build to build on purpose.
	var line bytes.Buffer
	if err := json.Compact(&line, raw); err != nil {
		return nil, fmt.Errorf("encode %s record: %w", rec.GetKind(), err)
	}
	return line.Bytes(), nil
}

func ptr[T any](v T) *T { return &v }
