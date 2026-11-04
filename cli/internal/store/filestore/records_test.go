package filestore_test

import (
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// Whatever the generator puts in a store comes back from the disk as it went in: the in-memory
// store and the on-disk store give the same answers, and the on-disk store shows the same state
// after it is closed and opened again, and after a crash, at every step. This is decode(encode(x)) == x
// for every record the generator builds, with every field set, the extremes of each number, text
// that troubles an encoding, and each variant of every union.
func TestEveryGeneratedRecordSurvivesBeingReadBack(t *testing.T) {
	for seed := uint64(100); seed < 130; seed++ {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			mem := fsys.NewMemFS()
			stores := []store.Store{memstore.New(), openOn(t, mem)}
			storetest.Differential(t, seed, 80, stores, func(step int) {
				if step%2 == 0 {
					mem = mem.CrashImage(step%4 == 0)
				}
				if err := stores[1].(*filestore.Store).Close(); err != nil {
					t.Fatal(err)
				}
				stores[1] = openOn(t, mem)
			})
		})
	}
}

// fill sets every field of m, choosing in each union the arm that variant selects, so that the
// variants 0 to 6 between them reach every arm of every union. It sets what the store holds and
// what a peer could not tell from real data: valid text, finite numbers, instants in range.
func fill(m protoreflect.Message, variant int) {
	md := m.Descriptor()
	for i := range md.Fields().Len() {
		fd := md.Fields().Get(i)
		if oneof := fd.ContainingOneof(); oneof != nil && !oneof.IsSynthetic() {
			if oneof.Fields().Get(variant%oneof.Fields().Len()) != fd {
				continue
			}
		}
		switch {
		case fd.IsMap():
			mp := m.Mutable(fd).Map()
			for k := range 2 {
				mp.Set(protoreflect.ValueOfString(fmt.Sprintf("k%d", k)).MapKey(), protoreflect.ValueOfString(fmt.Sprintf("v%d", k)))
			}
		case fd.IsList():
			list := m.Mutable(fd).List()
			for k := range 2 {
				if fd.Kind() == protoreflect.MessageKind {
					elem := list.NewElement()
					fill(elem.Message(), variant)
					list.Append(elem)
				} else {
					list.Append(scalar(fd, k+1))
				}
			}
		case fd.Kind() == protoreflect.MessageKind:
			sub := m.NewField(fd)
			switch fd.Message().FullName() {
			case "google.protobuf.Timestamp":
				sub = protoreflect.ValueOfMessage(timestamppb.New(time.Date(2026, 11, 1, 0, 0, 7+i, 123456789, time.UTC)).ProtoReflect())
			case "google.protobuf.Duration":
				sub = protoreflect.ValueOfMessage(durationpb.New(1500 * time.Millisecond).ProtoReflect())
			default:
				fill(sub.Message(), variant)
			}
			m.Set(fd, sub)
		default:
			m.Set(fd, scalar(fd, i+1))
		}
	}
	// The headline of a measurement is derived from its samples, and a file whose two disagree is refused.
	if md.FullName() == "vibesys.cli.v1.MeasureResult" {
		measure := m.Interface().(*cliv1.MeasureResult)
		measure.Value = core.Headline(measure.Samples)
	}
}

func scalar(fd protoreflect.FieldDescriptor, n int) protoreflect.Value {
	switch fd.Kind() {
	case protoreflect.BoolKind:
		return protoreflect.ValueOfBool(true)
	case protoreflect.EnumKind:
		values := fd.Enum().Values()
		return protoreflect.ValueOfEnum(values.Get(1 + n%(values.Len()-1)).Number())
	case protoreflect.Int32Kind:
		return protoreflect.ValueOfInt32(int32(n))
	case protoreflect.Int64Kind:
		return protoreflect.ValueOfInt64(int64(n))
	case protoreflect.Uint32Kind:
		return protoreflect.ValueOfUint32(uint32(n))
	case protoreflect.Uint64Kind:
		return protoreflect.ValueOfUint64(uint64(n))
	case protoreflect.DoubleKind:
		return protoreflect.ValueOfFloat64(float64(n) + 0.25)
	case protoreflect.StringKind:
		return protoreflect.ValueOfString(fmt.Sprintf("%s-%d", fd.Name(), n))
	}
	panic(fmt.Sprintf("field %s: no filler for kind %s; teach fill about it", fd.FullName(), fd.Kind()))
}

// Every field of every message an event carries is kept: a file with every field set, and each arm
// of each union, is read by the store and written again as the same message. A field that
// store.proto gains and the store does not carry fails here, instead of being dropped without a word.
func TestNoFieldOfAStoredMessageIsDropped(t *testing.T) {
	const name = "state/runs/run-a/1.json"
	for variant := range 7 {
		t.Run(fmt.Sprint(variant), func(t *testing.T) {
			event := &cliv1.Event{}
			fill(event.ProtoReflect(), variant)
			event.Sequence, event.RunId = 1, "run-a"
			want := &cliv1.EventFile{Format: 5, Event: event}
			data, err := protojson.Marshal(want)
			if err != nil {
				t.Fatal(err)
			}

			mem := fsys.NewMemFS()
			if err := openOn(t, mem).Close(); err != nil {
				t.Fatal(err)
			}
			mem.Put(name, data)
			read, err := openOn(t, mem).Events(t.Context(), "run-a", 0, 0)
			if err != nil || len(read) != 1 {
				t.Fatalf("Events = %+v, %v; the file was %s", read, err, data)
			}

			again := fsys.NewMemFS()
			if _, err := openOn(t, again).AppendEvent(t.Context(), read[0].EventDraft); err != nil {
				t.Fatalf("AppendEvent: %v", err)
			}
			var got cliv1.EventFile
			if err := protojson.Unmarshal(file(t, again, name), &got); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(want, &got) {
				t.Errorf("the file was written again as\n%s\nwant\n%s", protojson.Format(&got), protojson.Format(want))
			}
		})
	}
}

// seedFrom adds the file a finished script made, in each version a damaged copy might claim, to the
// corpus, so that the fuzzer starts from a file the store accepts and mutates towards the ones it
// should refuse.
func seedFrom(f *testing.F, name string) {
	f.Helper()
	mem := fsys.NewMemFS()
	s := openOnF(f, mem)
	for _, o := range script() {
		if err := o.run(f.Context(), s); err != nil {
			f.Fatalf("%s: %v", o.name, err)
		}
	}
	data, ok := mem.Files()[name]
	if !ok {
		f.Fatalf("the script made no file %s", name)
	}
	f.Add(data)
	f.Add(withVersion(data, "1"))
	f.Add(withVersion(data, "2"))
	f.Add(withVersion(data, "6"))
	f.Add([]byte(`{"format": 2}`))
	f.Add([]byte(`{}`))
	f.Add([]byte(`[]`))
	f.Add([]byte(nil))
}

func openOnF(f *testing.F, mem fsys.FS) *filestore.Store {
	f.Helper()
	s, err := filestore.Open(stateDir, filestore.WithFS(mem))
	if err != nil {
		f.Fatalf("Open: %v", err)
	}
	return s
}

// namesTheFile fails the test unless err is a refusal that names the file, as a failure of the kind
// a damaged or foreign file has.
func namesTheFile(t *testing.T, err error, name string) {
	t.Helper()
	var failure *core.Failure
	if !errors.As(err, &failure) || !strings.Contains(failure.Message, name) ||
		(failure.Kind != core.FailureInternal && failure.Kind != core.FailureIncompatible) {
		t.Fatalf("a file that cannot be read was refused as %v, want an internal or incompatible failure naming %s", err, name)
	}
}

// fuzzRecord holds the decoder of the file called name to three things: it never panics, it
// refuses what it cannot read with an error that names the file, and what it accepts it shows the
// same way after the store is opened again.
func fuzzRecord(f *testing.F, name string, read func(t *testing.T, s *filestore.Store) error) {
	f.Helper()
	seedFrom(f, name)
	f.Fuzz(func(t *testing.T, data []byte) {
		mem := populated(t)
		mem.Put(name, data)
		s, err := filestore.Open(stateDir, filestore.WithFS(mem))
		if err == nil {
			err = read(t, s)
		}
		if err != nil {
			namesTheFile(t, err, name)
			return
		}
		want := storetest.Observe(t, s, runs...)
		if got := storetest.Observe(t, openOn(t, mem.CrashImage(false)), runs...); !reflect.DeepEqual(got, want) {
			t.Fatalf("what the store accepted shows differently after it is opened again:\n%+v\nwant\n%+v", got, want)
		}
	})
}

func noRead(*testing.T, *filestore.Store) error { return nil }

func FuzzDecodeFormatFile(f *testing.F) { fuzzRecord(f, "state/format", noRead) }
func FuzzDecodeJobFile(f *testing.F)    { fuzzRecord(f, "state/jobs/j1.json", noRead) }
func FuzzDecodeResultFile(f *testing.F) { fuzzRecord(f, "state/results/j1.json", noRead) }
func FuzzDecodePlanFile(f *testing.F)   { fuzzRecord(f, "state/plans/1.json", noRead) }
func FuzzDecodePromotionFile(f *testing.F) {
	fuzzRecord(f, "state/promotions/1.json", noRead)
}
func FuzzDecodeFrontierFile(f *testing.F) { fuzzRecord(f, "state/frontier/2.json", noRead) }

// An event is read when it is asked for, not when the store opens.
func FuzzDecodeEventFile(f *testing.F) {
	fuzzRecord(f, eventFileName, func(t *testing.T, s *filestore.Store) error {
		_, err := s.Events(t.Context(), "run-a", 0, 0)
		return err
	})
}

// The index of stores has a file per task, named for a hash of the task.
func FuzzDecodeTaskFile(f *testing.F) {
	task := filestore.Task{Repository: "/repo", Name: "kv-store"}
	mem := fsys.NewMemFS()
	index, err := filestore.OpenIndex("index", filestore.WithFS(mem))
	if err != nil {
		f.Fatal(err)
	}
	if err := index.Add(task); err != nil {
		f.Fatal(err)
	}
	name, err := index.Entry(task)
	if err != nil || name == "" {
		f.Fatalf("Entry = %q, %v", name, err)
	}
	valid := mem.Files()[name]
	f.Add(valid)
	f.Add(withVersion(valid, "1"))
	f.Add(withVersion(valid, "3"))
	f.Add([]byte(`{"format": 2, "repository": "/other", "task": "other"}`))
	f.Add([]byte(`{}`))
	f.Fuzz(func(t *testing.T, data []byte) {
		image := mem.CrashImage(false)
		image.Put(name, data)
		index, err := filestore.OpenIndex("index", filestore.WithFS(image))
		if err != nil {
			t.Fatal(err)
		}
		tasks, err := index.Tasks()
		if err != nil {
			namesTheFile(t, err, name)
			return
		}
		if entry, err := index.Entry(tasks[0]); err != nil || entry != name {
			t.Fatalf("an accepted entry %v is filed as %q, %v, want %s", tasks, entry, err, name)
		}
	})
}
