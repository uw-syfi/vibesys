package accuracy

import (
	"context"
	"errors"
	"reflect"
	"testing"
)

func TestJournalCleansInReverseAndContinuesAfterFailure(t *testing.T) {
	journal := NewJournal()
	var order []string
	failMiddle := true
	for _, name := range []string{"first", "middle", "last"} {
		name := name
		if err := journal.Record(name, func(context.Context) error {
			order = append(order, name)
			if name == "middle" && failMiddle {
				return errors.New("injected")
			}
			return nil
		}); err != nil {
			t.Fatal(err)
		}
	}
	if err := journal.Cleanup(context.Background()); err == nil {
		t.Fatal("cleanup failure was discarded")
	}
	if !reflect.DeepEqual(order, []string{"last", "middle", "first"}) {
		t.Fatalf("cleanup order %v", order)
	}
	if journal.Active() != 1 {
		t.Fatalf("active=%d, want failed middle entry", journal.Active())
	}
	failMiddle = false
	if err := journal.Cleanup(context.Background()); err != nil {
		t.Fatal(err)
	}
	if journal.Active() != 0 || order[len(order)-1] != "middle" {
		t.Fatalf("retry did not clean failed entry: active=%d order=%v", journal.Active(), order)
	}
}

func TestJournalRejectsDuplicateActiveEntries(t *testing.T) {
	journal := NewJournal()
	cleanup := func(context.Context) error { return nil }
	if err := journal.Record("same", cleanup); err != nil {
		t.Fatal(err)
	}
	if err := journal.Record("same", cleanup); err == nil {
		t.Fatal("duplicate active cleanup entry was accepted")
	}
}
