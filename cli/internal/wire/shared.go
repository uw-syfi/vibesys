package wire

import (
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// The records daemons share through a file, a lock file or an annotation (see shared.proto) are
// protojson with the proto field names. They are read with unknown fields discarded, which is the
// rule that lets a later build add a field without breaking an earlier one; a record written
// before the messages existed has the same keys and reads as one.
var (
	sharedWriter = protojson.MarshalOptions{UseProtoNames: true}
	sharedReader = protojson.UnmarshalOptions{DiscardUnknown: true}
)

// LeaseRecord returns the contract's message for a lease holder.
func LeaseRecord(h core.LeaseHolder) *cliv1.LeaseRecord {
	return &cliv1.LeaseRecord{User: h.User, Host: h.Host, Pid: int32(h.PID), Job: string(h.Job), Since: timestamppb.New(h.Since.UTC())}
}

// ToLeaseHolder returns the holder a record names.
func ToLeaseHolder(r *cliv1.LeaseRecord) (core.LeaseHolder, error) {
	if err := checkShared(r); err != nil {
		return core.LeaseHolder{}, err
	}
	var since time.Time
	if r.GetSince() != nil {
		if err := r.GetSince().CheckValid(); err != nil {
			return core.LeaseHolder{}, invalid("since: %v", err)
		}
		since = r.GetSince().AsTime()
	}
	return core.LeaseHolder{User: r.GetUser(), Host: r.GetHost(), PID: int(r.GetPid()), Job: core.JobID(r.GetJob()), Since: since}, nil
}

// EncodeLeaseRecord writes a holder as the record kept beside its lease: one line of JSON.
func EncodeLeaseRecord(h core.LeaseHolder) ([]byte, error) {
	return sharedWriter.Marshal(LeaseRecord(h))
}

// DecodeLeaseRecord reads the holder out of a record kept beside a lease. A record that is empty,
// is not JSON, or breaks a rule of the message is a *core.Failure of kind invalid input that says
// so, and never a holder: the caller decides what an unreadable record means.
func DecodeLeaseRecord(data []byte) (core.LeaseHolder, error) {
	if len(data) == 0 {
		return core.LeaseHolder{}, invalid("lease record: empty")
	}
	var r cliv1.LeaseRecord
	if err := sharedReader.Unmarshal(data, &r); err != nil {
		return core.LeaseHolder{}, invalid("lease record: %v", err)
	}
	return ToLeaseHolder(&r)
}

// NodeReading returns the contract's message for what a node said and when.
func NodeReading(at time.Time, outputs map[string]string) *cliv1.NodeReading {
	return &cliv1.NodeReading{At: timestamppb.New(at.UTC()), Outputs: outputs}
}

// ToNodeReading returns when a node was read and what it printed.
func ToNodeReading(r *cliv1.NodeReading) (time.Time, map[string]string, error) {
	if err := checkShared(r); err != nil {
		return time.Time{}, nil, err
	}
	if err := r.GetAt().CheckValid(); err != nil {
		return time.Time{}, nil, invalid("at: %v", err)
	}
	return r.GetAt().AsTime(), r.GetOutputs(), nil
}

// EncodeNodeReading writes a reading as the record kept on the cluster.
func EncodeNodeReading(at time.Time, outputs map[string]string) ([]byte, error) {
	return sharedWriter.Marshal(NodeReading(at, outputs))
}

// DecodeNodeReading reads a node reading kept on the cluster. A record that is not one is a
// *core.Failure of kind invalid input, and never a reading.
func DecodeNodeReading(data []byte) (time.Time, map[string]string, error) {
	var r cliv1.NodeReading
	if err := sharedReader.Unmarshal(data, &r); err != nil {
		return time.Time{}, nil, invalid("node reading: %v", err)
	}
	return ToNodeReading(&r)
}

// checkShared holds a shared record to the rules of its message.
func checkShared(m proto.Message) error {
	if err := protovalidate.Validate(m); err != nil {
		return invalid("%v", err)
	}
	return nil
}
