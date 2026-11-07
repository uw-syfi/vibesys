package wire_test

import (
	"fmt"
	"math/rand/v2"
	"strings"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

// fill sets a message to random contents from its descriptor, so that a field added to a .proto is
// generated with no change here, and a mapper that forgets it fails the round trip.
func fill(r *rand.Rand, m protoreflect.Message) {
	if m.Descriptor().FullName() == "google.protobuf.Timestamp" {
		stamp := timestamppb.New(epoch.Add(time.Duration(r.Int64N(1 << 50))))
		m.Set(m.Descriptor().Fields().ByName("seconds"), protoreflect.ValueOfInt64(stamp.Seconds))
		m.Set(m.Descriptor().Fields().ByName("nanos"), protoreflect.ValueOfInt32(stamp.Nanos))
		return
	}
	if m.Descriptor().FullName() == "google.protobuf.Duration" {
		// At least the five minutes the contract asks of the interval between reports, which is also
		// positive, as a limit must be, with a part of a second to carry.
		m.Set(m.Descriptor().Fields().ByName("seconds"), protoreflect.ValueOfInt64(300+r.Int64N(1<<20)))
		m.Set(m.Descriptor().Fields().ByName("nanos"), protoreflect.ValueOfInt32(r.Int32N(1_000_000_000)))
		return
	}
	oneofs := m.Descriptor().Oneofs()
	for i := range oneofs.Len() {
		oneof := oneofs.Get(i)
		if !oneof.IsSynthetic() {
			set(r, m, oneof.Fields().Get(r.IntN(oneof.Fields().Len())))
		}
	}
	fields := m.Descriptor().Fields()
	for i := range fields.Len() {
		fd := fields.Get(i)
		chosen := fd.ContainingOneof() != nil && !fd.ContainingOneof().IsSynthetic()
		if !chosen && r.IntN(16) != 0 {
			set(r, m, fd)
		}
	}
}

func set(r *rand.Rand, m protoreflect.Message, fd protoreflect.FieldDescriptor) {
	switch {
	case fd.IsMap():
		panic(fmt.Sprintf("the contract grew a map: teach fill about %s", fd.FullName()))
	case fd.IsList():
		list := m.Mutable(fd).List()
		for range r.IntN(3) {
			if fd.Message() != nil {
				fill(r, list.AppendMutable().Message())
			} else {
				list.Append(scalar(r, fd))
			}
		}
	case fd.Message() != nil:
		child := m.Mutable(fd).Message()
		fill(r, child)
		// Outside a oneof, a message with nothing in it says what its absence says: the core keeps
		// one form of it, so the generator must not choose the other.
		if empty := proto.Size(child.Interface()) == 0; empty && fd.ContainingOneof() == nil {
			m.Clear(fd)
		}
	default:
		m.Set(fd, scalar(r, fd))
	}
}

// absolutePathFields are the string fields the contract requires to be absolute paths. Generating
// them as anything else would leave a message that holds one rejected unless the generator happened
// to skip the field, so whether a body was ever accepted would depend on the seed.
var absolutePathFields = map[string]bool{
	"marketplace": true, "home": true, "cli": true, "xdg_runtime_dir": true, "xdg_state_home": true,
	"executable": true, "runtime_directory": true, "location": true,
}

func scalar(r *rand.Rand, fd protoreflect.FieldDescriptor) protoreflect.Value {
	switch fd.Kind() {
	case protoreflect.BoolKind:
		return protoreflect.ValueOfBool(true)
	case protoreflect.StringKind:
		if absolutePathFields[string(fd.Name())] {
			return protoreflect.ValueOfString(fmt.Sprintf("/v%d", r.IntN(1000)))
		}
		if strings.HasSuffix(string(fd.Name()), "_digest") {
			return protoreflect.ValueOfString(fmt.Sprintf("%064x", r.Uint64()))
		}
		return protoreflect.ValueOfString(fmt.Sprintf("v%d", r.IntN(1000)))
	case protoreflect.BytesKind:
		return protoreflect.ValueOfBytes(fmt.Appendf(nil, "v%d", r.IntN(1000)))
	case protoreflect.Uint32Kind:
		return protoreflect.ValueOfUint32(1 + r.Uint32N(1000))
	case protoreflect.Uint64Kind:
		return protoreflect.ValueOfUint64(1 + r.Uint64N(1000))
	case protoreflect.Int32Kind:
		return protoreflect.ValueOfInt32(r.Int32N(1000))
	case protoreflect.DoubleKind:
		return protoreflect.ValueOfFloat64(float64(r.IntN(1000)) / 8)
	case protoreflect.EnumKind:
		values := fd.Enum().Values()
		return protoreflect.ValueOfEnum(values.Get(1 + r.IntN(values.Len()-1)).Number())
	}
	panic(fmt.Sprintf("the contract grew a %s: teach fill about %s", fd.Kind(), fd.FullName()))
}

// generated returns a random message of m's type.
func generated[M proto.Message](r *rand.Rand, m M) M {
	fill(r, m.ProtoReflect())
	return m
}

func encoded(m proto.Message) []byte {
	data, err := proto.MarshalOptions{Deterministic: true}.Marshal(m)
	if err != nil {
		panic(err)
	}
	return data
}
