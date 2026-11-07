package core

import "slices"

// ModelPolicy is the host's rule on which models agents may run on, a fact of the host that the shell
// reads and hands to Decide with each input, so the core reads no file. The zero value restricts
// nothing.
type ModelPolicy struct {
	// Allowed is the models the host allows, in its order. Empty, with Closed unset, allows every model.
	Allowed []ModelName
	// Closed says the host has a policy that could not be read: it allows nothing until it can, so a
	// broken file never reads as no file.
	Closed bool
	// Known is the models each harness runs, its default first, as its tool knows them. It is how an
	// agent that names no model is given one: a harness absent from it has no model to give.
	Known map[Harness][]ModelName
}

// Restricts says the policy limits the models agents may run on.
func (p ModelPolicy) Restricts() bool { return p.Closed || len(p.Allowed) > 0 }

// Resolve gives the model an agent on harness runs on when it asks for model, and says whether the
// policy allows it. Where the policy restricts nothing the model is left as asked, empty included: the
// harness's own default is no concern of a host that sets no rule. Where it restricts, the answer is
// always a concrete model in Allowed or a refusal: an empty model becomes the first allowed model the
// harness runs, and one the harness is not known to run is refused, never allowed by default.
func (p ModelPolicy) Resolve(harness Harness, model ModelName) (ModelName, bool) {
	switch {
	case !p.Restricts():
		return model, true
	case p.Closed:
		return "", false
	case model != "":
		return model, slices.Contains(p.Allowed, model)
	}
	known := p.Known[harness]
	i := slices.IndexFunc(p.Allowed, func(m ModelName) bool { return slices.Contains(known, m) })
	if i < 0 {
		return "", false
	}
	return p.Allowed[i], true
}

// Permits is the part of a run's allowance the policy allows: the choices that name a model it lists.
// A choice with no model is the harness's own default, which no policy that restricts models can say it
// allows, so it is not in the part. Where the policy restricts nothing, it is the whole allowance.
func (p ModelPolicy) Permits(allowance []AgentChoice) []AgentChoice {
	if !p.Restricts() {
		return allowance
	}
	var out []AgentChoice
	for _, c := range allowance {
		if !p.Closed && c.Model != "" && slices.Contains(p.Allowed, c.Model) {
			out = append(out, c)
		}
	}
	return out
}

// refuseModel makes the receipt that says the policy refused a model.
func (t *tx) refuseModel(r *Receipt, asked ModelName) {
	r.Refusal, r.Model = RefuseModelNotAllowed, asked
	if !t.in.Models.Closed {
		r.AllowedModels = slices.Clone(t.in.Models.Allowed)
	}
}

// resolveChoice applies the policy to one harness and model of a start or a spawn.
func (t *tx) resolveChoice(r *Receipt, harness Harness, model ModelName) (ModelName, bool) {
	resolved, ok := t.in.Models.Resolve(harness, model)
	if !ok {
		t.refuseModel(r, model)
	}
	return resolved, ok
}
