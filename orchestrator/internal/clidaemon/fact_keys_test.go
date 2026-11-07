package clidaemon_test

import (
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/protobuf/reflect/protoreflect"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
)

// fill sets every singular field of m to a value that is not the zero value, a message to the same
// below the depth, and the first arm of a union, so that whatever keys a fact is held by is set.
func fill(m protoreflect.Message, depth int) {
	fields := m.Descriptor().Fields()
	for i := range fields.Len() {
		fd := fields.Get(i)
		if fd.IsList() || fd.IsMap() {
			continue
		}
		if o := fd.ContainingOneof(); o != nil && !o.IsSynthetic() && o.Fields().Get(0) != fd {
			continue
		}
		switch fd.Kind() {
		case protoreflect.MessageKind, protoreflect.GroupKind:
			if depth > 0 {
				fill(m.Mutable(fd).Message(), depth-1)
			}
		case protoreflect.EnumKind:
			values := fd.Enum().Values()
			m.Set(fd, protoreflect.ValueOfEnum(values.Get(min(1, values.Len()-1)).Number()))
		case protoreflect.StringKind:
			m.Set(fd, protoreflect.ValueOfString("x"))
		case protoreflect.BytesKind:
			m.Set(fd, protoreflect.ValueOfBytes([]byte("x")))
		case protoreflect.BoolKind:
			m.Set(fd, protoreflect.ValueOfBool(true))
		case protoreflect.Int32Kind, protoreflect.Sint32Kind, protoreflect.Sfixed32Kind:
			m.Set(fd, protoreflect.ValueOfInt32(1))
		case protoreflect.Int64Kind, protoreflect.Sint64Kind, protoreflect.Sfixed64Kind:
			m.Set(fd, protoreflect.ValueOfInt64(1))
		case protoreflect.Uint32Kind, protoreflect.Fixed32Kind:
			m.Set(fd, protoreflect.ValueOfUint32(1))
		case protoreflect.Uint64Kind, protoreflect.Fixed64Kind:
			m.Set(fd, protoreflect.ValueOfUint64(1))
		case protoreflect.FloatKind:
			m.Set(fd, protoreflect.ValueOfFloat32(1))
		case protoreflect.DoubleKind:
			m.Set(fd, protoreflect.ValueOfFloat64(1))
		}
	}
}

// A fact of every kind the task watch can stream has a key to be held by. A session that cannot key
// a fact calls the whole stream unreadable, so a kind the daemon gains and the session does not know
// would cut the orchestrator off from a task as soon as one is written. The kinds are read from the
// contract, so a kind added to it is checked here without a change to this test.
func TestEveryKindOfFactTheDaemonStreamsHasAKey(t *testing.T) {
	arms := (&pb.TaskFact{}).ProtoReflect().Descriptor().Oneofs().ByName("fact")
	if arms == nil || arms.Fields().Len() == 0 {
		t.Fatal("TaskFact has no kinds of fact")
	}
	seen := map[string]protoreflect.Name{}
	for i := range arms.Fields().Len() {
		arm := arms.Fields().Get(i)
		fact := &pb.TaskFact{}
		fill(fact.ProtoReflect().Mutable(arm).Message(), 3)
		key, err := clidaemon.FactKey(fact)
		if err != nil {
			t.Errorf("a %s fact has no key: %v", arm.Name(), err)
			continue
		}
		if key != string(arm.Name()) && !strings.HasPrefix(key, string(arm.Name())+"/") {
			t.Errorf("the key of a %s fact is %q: a key starts with the kind, so two kinds never share one", arm.Name(), key)
		}
		if other, dup := seen[key]; dup {
			t.Errorf("%s and %s facts both have the key %q", other, arm.Name(), key)
		}
		seen[key] = arm.Name()
	}
}
