package search

import (
	"cmp"
	"slices"
	"time"
	"unicode/utf8"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// MaxStateBytes bounds the entire encoded state, including identity and health.
// A response wrapper adds at most five bytes, well within the 4 MiB receive limit.
const MaxStateBytes = 256 << 10

// Detail bounds display text on a UTF-8 boundary; it never carries a typed fact.
func Detail(detail string) string {
	const maxBytes = 4096 // Unreadable.detail's schema bound; pinned by the boundary tests.
	if len(detail) <= maxBytes {
		return detail
	}
	end := maxBytes
	for !utf8.RuneStart(detail[end]) {
		end--
	}
	return detail[:end]
}

// lastToGo is the age of an entry that is dropped for bytes after every entry that has a time.
var lastToGo = time.Date(9999, 12, 31, 0, 0, 0, 0, time.UTC)

type byteEntry struct {
	field   protoreflect.FieldDescriptor
	index   int
	value   protoreflect.Value
	at      time.Time
	bytes   int
	dropped bool
}

func boundBytes(s *pb.SearchState, v View) {
	size := proto.Size(s)
	if size <= MaxStateBytes {
		return
	}
	s.Truncated = true
	size = proto.Size(s)
	jobs, commits := ages(v)
	planned := map[uint32]time.Time{}
	for _, p := range s.Plans {
		planned[p.Number] = p.GetCreatedAt().AsTime()
	}
	var entries []*byteEntry
	m := s.ProtoReflect()
	m.Range(func(fd protoreflect.FieldDescriptor, value protoreflect.Value) bool {
		// The measured history is compact next to the rows it is derived from, so it is trimmed point by
		// point after them (shrinkTrajectory), never dropped as a whole entry.
		if !fd.IsList() || fd.Name() == "series" {
			return true
		}
		list := value.List()
		for i := 0; i < list.Len(); i++ {
			value := list.Get(i)
			msg := value.Message().Interface()
			entries = append(entries, &byteEntry{field: fd, index: i, value: value, at: entryAge(msg, jobs, commits, planned), bytes: protowire.SizeTag(protowire.Number(fd.Number())) + protowire.SizeBytes(proto.Size(msg))})
		}
		return true
	})
	slices.SortFunc(entries, func(a, b *byteEntry) int {
		if c := a.at.Compare(b.at); c != 0 {
			return c
		}
		if c := cmp.Compare(a.field.Number(), b.field.Number()); c != 0 {
			return c
		}
		return cmp.Compare(a.index, b.index)
	})
	for _, entry := range entries {
		if size <= MaxStateBytes {
			break
		}
		entry.dropped = true
		size -= entry.bytes
	}
	// Rebuild each list in its original presentation order.
	slices.SortFunc(entries, func(a, b *byteEntry) int {
		if c := cmp.Compare(a.field.Number(), b.field.Number()); c != 0 {
			return c
		}
		return cmp.Compare(a.index, b.index)
	})
	for _, entry := range entries {
		if entry.index == 0 {
			m.Mutable(entry.field).List().Truncate(0)
		}
		if !entry.dropped {
			m.Mutable(entry.field).List().Append(entry.value)
		}
	}
	// A removed job also removes its dependent rows.
	kept := map[string]bool{}
	for _, j := range s.Jobs {
		kept[j.Id] = true
	}
	s.Results = slices.DeleteFunc(s.Results, func(r *cli.Result) bool { return !kept[r.JobId] })
	s.Troubles = slices.DeleteFunc(s.Troubles, func(r *cli.JobTrouble) bool { return !kept[r.JobId] })
	// A plan and its idea are one entry to a reader. They are as old as each other and the plan sorts
	// first, so the plan goes first and the cut can leave only an idea whose plan went: it goes too.
	planKept := map[uint32]bool{}
	for _, p := range s.Plans {
		planKept[p.Number] = true
	}
	s.Ideas = slices.DeleteFunc(s.Ideas, func(i *pb.IdeaView) bool { return !planKept[i.PlanNumber] })
	shrinkTrajectory(s, func() bool { return proto.Size(s) <= MaxStateBytes })
	// Head can contain a result of arbitrary size. Retain health and identity even
	// when that singleton cannot fit after every historical entry was removed.
	if proto.Size(s) > MaxStateBytes {
		s.Head = nil
	}
	if proto.Size(s) > MaxStateBytes {
		panic("search: identity and health exceed the state byte budget")
	}
}

func ages(v View) (map[string]time.Time, map[string]time.Time) {
	jobs, commits := map[string]time.Time{}, map[string]time.Time{}
	for _, f := range v.Facts {
		if j := f.GetJob(); j != nil {
			jobs[j.Id] = j.GetSubmittedAt().AsTime()
		}
		if r := f.GetResult(); r != nil && r.GetMeasure() != nil {
			at := r.GetRecordedAt().AsTime()
			if previous, ok := commits[r.Commit]; !ok || at.After(previous) {
				commits[r.Commit] = at
			}
		}
	}
	return jobs, commits
}
func entryAge(m proto.Message, jobs, commits map[string]time.Time, plans map[uint32]time.Time) time.Time {
	switch x := m.(type) {
	case *cli.Job:
		return x.GetSubmittedAt().AsTime()
	case *cli.Result:
		return x.GetRecordedAt().AsTime()
	case *cli.Judgment:
		return commits[x.Commit]
	case *cli.Plan:
		return x.GetCreatedAt().AsTime()
	case *cli.PromotionRecord:
		return x.GetPromotion().GetAt().AsTime()
	case *cli.JobTrouble:
		return jobs[x.JobId]
	case *pb.IdeaView:
		// An idea goes with its plan: it is as old as the plan.
		return plans[x.GetPlanNumber()]
	case *pb.ObjectiveBounds, *cli.Workstream, *pb.WorkstreamView:
		// A run's declared bounds, its workstreams and their counts are a few entries each, stated
		// once and revised rarely: they go last, after every job, result and judgment that is older
		// than the end of time.
		return lastToGo
	default:
		panic("search: unknown historical entry")
	}
}
