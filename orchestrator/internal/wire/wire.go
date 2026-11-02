// Package wire is the boundary between the contract and the core. Bytes from a client, a sidecar or
// the store are parsed here, once, into core types: an unknown field, a rule of the contract broken,
// or a value the core has no name for is an error that names what is wrong. Core types are encoded
// here on the way out, so the core never sees a generated type.
//
// Encode then decode gives back the value that went in, for everything the core emits. The
// simulation replays through these bytes, so a field that does not survive shows up as a replay
// that differs from the live state.
package wire

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// errorList gathers what is wrong with a message, so that a mapper needs no error at every field.
type errorList struct{ errs []error }

func (l *errorList) addf(format string, args ...any) {
	l.errs = append(l.errs, fmt.Errorf(format, args...))
}

func (l *errorList) err() error { return errors.Join(l.errs...) }

// parse decodes bytes into m and holds the result to the contract.
func parse(data []byte, m proto.Message) error {
	if err := proto.Unmarshal(data, m); err != nil {
		return fmt.Errorf("decode %s: %w", m.ProtoReflect().Descriptor().Name(), err)
	}
	return check(m)
}

// check holds a message to the contract: its rules, and no field this build does not know.
func check(m proto.Message) error {
	name := m.ProtoReflect().Descriptor().Name()
	if field := unknownField(m.ProtoReflect()); field != "" {
		return fmt.Errorf("%s: unknown field in %s", name, field)
	}
	if err := protovalidate.Validate(m); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

// unknownField names a message that holds a field unknown to this build, anywhere in m.
func unknownField(m protoreflect.Message) string {
	if len(m.GetUnknown()) > 0 {
		return string(m.Descriptor().FullName())
	}
	var found string
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		switch {
		case fd.Message() == nil || fd.IsMap():
			return true
		case fd.IsList():
			for i := range v.List().Len() {
				if found = unknownField(v.List().Get(i).Message()); found != "" {
					return false
				}
			}
		default:
			found = unknownField(v.Message())
		}
		return found == ""
	})
	return found
}

func when(t time.Time) *timestamppb.Timestamp {
	if t.IsZero() {
		return nil
	}
	return timestamppb.New(t)
}

func moment(ts *timestamppb.Timestamp, errs *errorList) time.Time {
	if ts == nil {
		return time.Time{}
	}
	if err := ts.CheckValid(); err != nil {
		errs.addf("timestamp: %w", err)
		return time.Time{}
	}
	return ts.AsTime()
}

// digest identifies a command's content, leaving out the id it arrived under.
func digest(c *pb.Command) (core.Digest, error) {
	body := proto.Clone(c).(*pb.Command)
	body.Id = ""
	data, err := proto.MarshalOptions{Deterministic: true}.Marshal(body)
	if err != nil {
		return "", fmt.Errorf("digest of command %s: %w", c.GetId(), err)
	}
	sum := sha256.Sum256(data)
	return core.Digest(hex.EncodeToString(sum[:])), nil
}

// sessionRef and session convert a session's name.
func sessionRef(s core.Session) *pb.SessionRef {
	return &pb.SessionRef{AgentId: string(s.Agent), Session: s.N}
}

func session(s *pb.SessionRef) core.Session {
	return core.Session{Agent: core.AgentID(s.GetAgentId()), N: s.GetSession()}
}

// describe names a oneof that came through empty or unknown, which check rules out for the required
// ones. It keeps a mapper total.
func describe(kind string, v any) error { return fmt.Errorf("%s: unexpected body %T", kind, v) }
