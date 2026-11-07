package wire_test

import (
	"fmt"
	"math/rand"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// A run can run on any number of models, with names of any length, and the message still meets the
// contract: the models and each bucket's by_model keep the most costly ones up to the limit, a name
// is cut at a character boundary, the message says it left something out when it did, and what is not
// a list of models (the total, each bucket's cost) still counts every turn.
func TestCostMessageOfAnyNumberOfModelsMeetsTheContract(t *testing.T) {
	begin := time.Date(2026, time.November, 1, 9, 0, 0, 0, time.UTC)
	r := rand.New(rand.NewSource(5))
	// Names mix one-byte and three-byte characters, so a cut at the limit can fall inside one.
	name := func(i int) string {
		n := r.Intn(3 * wire.MaxModelNameBytes)
		var b strings.Builder
		fmt.Fprintf(&b, "m%03d-", i)
		for b.Len() < n {
			b.WriteString([]string{"x", "世"}[r.Intn(2)])
		}
		return b.String()
	}
	for range 60 {
		models := 1 + r.Intn(3*wire.MaxCostModels)
		var l cost.Ledger
		for i := range models {
			a := core.Agent{ID: core.AgentID(fmt.Sprintf("a%03d", i)), Role: core.RoleWorker, Spec: core.AgentSpec{Harness: core.Codex, Model: core.ModelName(name(i))}}
			l.Agent(a)
			l.Add(core.Turn{
				ID: core.TurnID(fmt.Sprintf("t%03d", i)), Agent: a.ID, Ordinal: 1, Status: core.TurnCompleted,
				Usage:   core.Usage{InputTokens: tokensOf(uint64(1 + r.Intn(100))), CostUSD: dollarsOf(float64(1+r.Intn(50)) / 4)},
				EndedAt: begin.Add(time.Duration(i) * time.Minute),
			}, a)
		}
		v := l.View(core.Run{ID: "run-1", CreatedAt: begin}, begin.Add(time.Duration(models)*time.Minute))
		msg := wire.CostMessage(v, 1)
		if err := wire.Validate(msg); err != nil {
			t.Fatalf("%d models: the contract refuses the message: %v", models, err)
		}
		kept := min(models, wire.MaxCostModels)
		if len(msg.Models) != kept {
			t.Fatalf("%d models: %d kept, want %d", models, len(msg.Models), kept)
		}
		longKept := false
		for _, m := range v.Models[:kept] {
			longKept = longKept || len(m.Model.Model) > wire.MaxModelNameBytes
		}
		if want := models > wire.MaxCostModels || longKept; msg.ModelsTruncated != want {
			t.Fatalf("%d models: models_truncated = %v, want %v", models, msg.ModelsTruncated, want)
		}
		inList := map[string]bool{}
		for i, m := range msg.Models {
			if !utf8.ValidString(m.Model.Model) || len(m.Model.Model) > wire.MaxModelNameBytes {
				t.Fatalf("model name of %d bytes, valid=%v", len(m.Model.Model), utf8.ValidString(m.Model.Model))
			}
			if !strings.HasPrefix(string(v.Models[i].Model.Model), m.Model.Model) {
				t.Fatalf("name %q is not a cut of %q", m.Model.Model, v.Models[i].Model.Model)
			}
			if m.TotalTokens != cost.TotalTokens(v.Models[i].Usage) {
				t.Fatalf("model %d: %d tokens, want %d", i, m.TotalTokens, cost.TotalTokens(v.Models[i].Usage))
			}
			inList[m.Model.Harness.String()+"/"+m.Model.Model] = true
		}
		if msg.Total.GetCostUsd() != *v.Total.CostUSD {
			t.Fatalf("the total changed: %v, want %v", msg.Total.GetCostUsd(), *v.Total.CostUSD)
		}
		for i, b := range msg.Series.Buckets {
			if b.CostUsd != v.Series.Buckets[i].Cost {
				t.Fatalf("bucket %d cost %v, want every model's %v", i, b.CostUsd, v.Series.Buckets[i].Cost)
			}
			for _, s := range b.ByModel {
				if !inList[s.Model.Harness.String()+"/"+s.Model.Model] {
					t.Fatalf("bucket %d has spend of %q, which the models do not list", i, s.Model.Model)
				}
			}
		}
	}
}
