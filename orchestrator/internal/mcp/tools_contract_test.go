package mcp_test

import (
	"encoding/json"
	"flag"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go/buf/validate"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/mcp"
	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

var update = flag.Bool("update", false, "rewrite the tools/list golden")

// The golden pins what an agent reads when it lists the server's tools: names, descriptions and
// every argument's schema. A change to a proto message that a tool takes, or to the generator, shows
// here as the words that agents would see differently.
func TestToolsListIsPinnedByAGoldenForEachRole(t *testing.T) {
	for _, role := range []core.AgentRole{core.RoleLead, core.RoleWorker} {
		t.Run(string(role), func(t *testing.T) {
			result := rpc(t, newServer(role, &fakeDaemon{}), 1, "tools/list", map[string]any{})["result"]
			got, err := json.MarshalIndent(result, "", "  ")
			if err != nil {
				t.Fatal(err)
			}
			got = append(got, '\n')
			path := filepath.Join("testdata", "tools_list_"+string(role)+".golden.json")
			if *update {
				if err := os.WriteFile(path, got, 0o644); err != nil {
					t.Fatal(err)
				}
			}
			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != string(want) {
				t.Fatalf("tools/list changed; if the new words are meant, run go test ./internal/mcp -run Golden -update and read the diff.\ngot:\n%s", got)
			}
		})
	}
}

func TestTheServerSaysTheNameTheHostRegistersItUnder(t *testing.T) {
	info := rpc(t, newServer(core.RoleLead, &fakeDaemon{}), 1, "initialize", map[string]any{})["result"].(map[string]any)["serverInfo"].(map[string]any)
	if info["name"] != mcp.ServerName {
		t.Fatalf("serverInfo is %v, want the name %q", info, mcp.ServerName)
	}
}

// toolMessages are the messages of the contract that are a tool's input, found by the option that
// makes them one, so a tool added in a proto is tested with no edit here.
func toolMessages(t testing.TB) map[string]protoreflect.MessageDescriptor {
	t.Helper()
	out := map[string]protoreflect.MessageDescriptor{}
	protoregistry.GlobalFiles.RangeFilesByPackage("vibesys.orchestrator.v1", func(fd protoreflect.FileDescriptor) bool {
		for i := 0; i < fd.Messages().Len(); i++ {
			md := fd.Messages().Get(i)
			if tool, _ := proto.GetExtension(md.Options(), pb.E_McpTool).(*pb.McpTool); tool != nil {
				out[tool.GetName()] = md
			}
		}
		return true
	})
	if len(out) == 0 {
		t.Fatal("no message of the contract is a tool's input")
	}
	return out
}

func isHidden(fd protoreflect.FieldDescriptor) bool {
	h, _ := proto.GetExtension(fd.Options(), pb.E_ToolFieldHidden).(bool)
	return h
}

func rulesOf(fd protoreflect.FieldDescriptor) *validate.FieldRules {
	r, _ := proto.GetExtension(fd.Options(), validate.E_Field).(*validate.FieldRules)
	return r
}

// needed says the proto's own rules refuse the field's empty value.
func needed(fd protoreflect.FieldDescriptor) bool {
	r := rulesOf(fd)
	return r.GetRequired() || r.GetString().GetMinLen() > 0 || slices.Contains(r.GetEnum().GetNotIn(), 0)
}

// valid draws arguments that satisfy a message's descriptor: every needed field, a random part of
// the others, and exactly one field of each group. own names the field of the group called body.
func valid(r *rand.Rand, md protoreflect.MessageDescriptor, own string) map[string]any {
	obj := map[string]any{}
	chosen := map[protoreflect.Name]bool{}
	for i := 0; i < md.Oneofs().Len(); i++ {
		od := md.Oneofs().Get(i)
		if od.IsSynthetic() {
			continue
		}
		pick := od.Fields().Get(r.IntN(od.Fields().Len()))
		if own != "" && od.Name() == "body" {
			pick = od.Fields().ByName(protoreflect.Name(own))
		}
		chosen[pick.Name()] = true
	}
	for i := 0; i < md.Fields().Len(); i++ {
		fd := md.Fields().Get(i)
		if isHidden(fd) {
			continue
		}
		if oneof := fd.ContainingOneof(); oneof != nil && !oneof.IsSynthetic() {
			if !chosen[fd.Name()] {
				continue
			}
		} else if !needed(fd) && r.IntN(10) < 3 {
			continue
		}
		obj[string(fd.Name())] = value(r, fd)
	}
	return obj
}

func value(r *rand.Rand, fd protoreflect.FieldDescriptor) any {
	switch fd.Kind() {
	case protoreflect.StringKind:
		if pattern := rulesOf(fd).GetString().GetPattern(); pattern != "" {
			return matching(r, pattern)
		}
		return fmt.Sprintf("v%d é {{ %%s \"q\"", r.Uint32())
	case protoreflect.BoolKind:
		return r.IntN(2) == 0
	case protoreflect.Uint32Kind:
		return r.Uint32()
	case protoreflect.Uint64Kind:
		return r.Uint64() >> 12 // a JSON number an IEEE double holds exactly.
	case protoreflect.EnumKind:
		var skip []protoreflect.EnumNumber
		for _, n := range rulesOf(fd).GetEnum().GetNotIn() {
			skip = append(skip, protoreflect.EnumNumber(n))
		}
		all := names.Spellings(fd.Enum(), skip...)
		return all[r.IntN(len(all))]
	case protoreflect.MessageKind:
		return valid(r, fd.Message(), "")
	}
	panic("a kind the contract's tools do not take: " + fd.Kind().String())
}

// taskNamePattern is the rule of a task's name, which DeclareTask and the declare_task tool share.
const taskNamePattern = "^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$"

// matching draws a string that satisfies a pattern a tool's field carries. A pattern is written
// out here, and an unknown one fails, so a new pattern is a decision of this test and not a guess.
func matching(r *rand.Rand, pattern string) string {
	switch pattern {
	case taskNamePattern:
		const first, rest = "abcXYZ019_-", "abcXYZ019_-."
		name := []byte{first[r.IntN(len(first))]}
		for range r.IntN(20) {
			name = append(name, rest[r.IntN(len(rest))])
		}
		return string(name)
	}
	panic("a pattern the contract's tools carry and this test cannot draw from: " + pattern)
}

// callerFor is a role that has the tool, with the roles of the tool's callers drawn at random.
func callerFor(t testing.TB, name string, r *rand.Rand) core.AgentRole {
	t.Helper()
	var holders []core.AgentRole
	for _, role := range roles {
		if slices.Contains(listed(t, newServer(role, &fakeDaemon{})), name) {
			holders = append(holders, role)
		}
	}
	if len(holders) == 0 {
		t.Fatalf("no role is offered the tool %s that the contract defines", name)
	}
	return holders[r.IntN(len(holders))]
}

func ownOf(name string, role core.AgentRole) string {
	if name == "post_handoff" {
		return string(role)
	}
	return ""
}

func TestArgumentsThatSatisfyTheDescriptorAreAcceptedAndSentWhole(t *testing.T) {
	r := rand.New(rand.NewPCG(11, 12))
	for name, md := range toolMessages(t) {
		for range thinnedUnderRace(300) {
			role := callerFor(t, name, r)
			args := valid(r, md, ownOf(name, role))
			d := &fakeDaemon{}
			text, isErr := call(t, newServer(role, d), 1, name, args)
			if isErr {
				raw, _ := json.Marshal(args)
				t.Fatalf("%s as %s refused %s: %s", name, role, raw, text)
			}
			if name != "list_agents" && len(d.sent) != 1 {
				t.Fatalf("%s sent %d commands", name, len(d.sent))
			}
		}
	}
}

// paths lists the path of each field of the arguments that is present, to remove or break.
func paths(obj map[string]any, prefix []string) [][]string {
	var out [][]string
	for k, v := range obj {
		path := append(slices.Clone(prefix), k)
		out = append(out, path)
		if child, ok := v.(map[string]any); ok {
			out = append(out, paths(child, path)...)
		}
	}
	return out
}

func at(obj map[string]any, path []string) map[string]any {
	for _, k := range path[:len(path)-1] {
		obj = obj[k].(map[string]any)
	}
	return obj
}

func clone(v map[string]any) map[string]any {
	raw, _ := json.Marshal(v)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return out
}

// fieldAt finds the descriptor of the field a path names.
func fieldAt(md protoreflect.MessageDescriptor, path []string) protoreflect.FieldDescriptor {
	var fd protoreflect.FieldDescriptor
	for _, k := range path {
		fd = md.Fields().ByName(protoreflect.Name(k))
		md = fd.Message()
	}
	return fd
}

func TestEveryMissingNeededFieldAndEveryBadValueIsRefusedByName(t *testing.T) {
	r := rand.New(rand.NewPCG(21, 22))
	for name, md := range toolMessages(t) {
		for range thinnedUnderRace(60) {
			role := callerFor(t, name, r)
			base := valid(r, md, ownOf(name, role))
			for _, path := range paths(base, nil) {
				field := strings.Join(path, ".")
				fd := fieldAt(md, path)
				args := clone(base)
				parent := at(args, path)
				last := path[len(path)-1]
				// A missing field that the rules need is refused, naming it.
				if needed(fd) {
					delete(parent, last)
					d := &fakeDaemon{}
					text, isErr := call(t, newServer(role, d), 1, name, args)
					if !isErr || !strings.Contains(text, "`"+field+"`") || len(d.sent) != 0 {
						t.Fatalf("%s without %s: error=%v sent=%d %q", name, field, isErr, len(d.sent), text)
					}
					continue
				}
				// A value of the wrong kind is refused, naming the field and the kind.
				args = clone(base)
				parent = at(args, path)
				switch fd.Kind() {
				case protoreflect.EnumKind:
					parent[last] = "no_such_value"
					text, isErr := call(t, newServer(role, &fakeDaemon{}), 1, name, args)
					if !isErr || !strings.Contains(text, "`"+field+"`") || !strings.Contains(text, "no_such_value") ||
						!strings.Contains(text, names.Spellings(fd.Enum())[0]) {
						t.Fatalf("%s with a bad %s: %q", name, field, text)
					}
				case protoreflect.StringKind, protoreflect.BoolKind, protoreflect.Uint32Kind, protoreflect.Uint64Kind, protoreflect.MessageKind:
					parent[last] = []any{1}
					text, isErr := call(t, newServer(role, &fakeDaemon{}), 1, name, args)
					if !isErr || !strings.Contains(text, "`"+field+"`") || !strings.Contains(text, "must be") {
						t.Fatalf("%s with a list for %s: %q", name, field, text)
					}
				}
			}
			// A key the message does not have is refused at every level, and listed against what is.
			for _, path := range append([][]string{nil}, objectPaths(base, nil)...) {
				args := clone(base)
				target := args
				for _, k := range path {
					target = target[k].(map[string]any)
				}
				target["not_an_argument"] = "x"
				text, isErr := call(t, newServer(role, &fakeDaemon{}), 1, name, args)
				want := "unknown field `" + strings.Join(append(slices.Clone(path), "not_an_argument"), ".") + "`"
				if !isErr || !strings.Contains(text, want) {
					t.Fatalf("%s with an extra key under %v: %q, want %q", name, path, text, want)
				}
			}
		}
	}
}

func objectPaths(obj map[string]any, prefix []string) [][]string {
	var out [][]string
	for k, v := range obj {
		if child, ok := v.(map[string]any); ok {
			p := append(slices.Clone(prefix), k)
			out = append(out, p)
			out = append(out, objectPaths(child, p)...)
		}
	}
	return out
}

// Giving two fields of a group, or none, is refused with the fields to choose from.
func TestAGroupOfWhichOneIsToBeGivenIsRefusedWhenItHasNoneOrTwo(t *testing.T) {
	worker := newServer(core.RoleWorker, &fakeDaemon{})
	candidate := map[string]any{"commit": "c", "parent_commit": "p", "branch": "b", "checks": "k", "measurement": "m"}
	cases := map[string]map[string]any{
		"no outcome":   {"claim": "x", "worker": map[string]any{}},
		"two outcomes": {"claim": "x", "worker": map[string]any{"candidate": candidate, "no_candidate": map[string]any{"blocker": "why"}}},
	}
	for name, args := range cases {
		text, isErr := call(t, worker, 1, "post_handoff", args)
		if !isErr || !strings.Contains(text, "exactly one of candidate, no_candidate") {
			t.Errorf("%s: %q", name, text)
		}
	}
}

// A handoff is the caller's own: a missing or another role's body is refused in the words of the
// caller's role, before anything is sent.
func TestAHandoffWithNoBodyOrAnotherRolesBodyIsRefusedSayingWhichToFill(t *testing.T) {
	subagents := []core.AgentRole{core.RolePlanner, core.RoleWorker, core.RoleJudge, core.RoleCombiner, core.RoleProfiler}
	r := rand.New(rand.NewPCG(31, 32))
	md := toolMessages(t)["post_handoff"]
	for _, own := range subagents {
		for _, other := range subagents {
			d := &fakeDaemon{}
			args := valid(r, md, string(other))
			text, isErr := call(t, newServer(own, d), 1, "post_handoff", args)
			switch {
			case own == other && isErr:
				t.Errorf("a %s was refused its own body: %q", own, text)
			case own != other && (!isErr || !strings.Contains(text, "You are a "+string(own)) || !strings.Contains(text, "`"+string(other)+"`") || len(d.sent) != 0):
				t.Errorf("a %s sent the body of a %s: error=%v sent=%d %q", own, other, isErr, len(d.sent), text)
			}
		}
		args := map[string]any{"claim": "x"}
		text, isErr := call(t, newServer(own, &fakeDaemon{}), 1, "post_handoff", args)
		if !isErr || !strings.Contains(text, "no `"+string(own)+"` field") {
			t.Errorf("a %s with no body: %q", own, text)
		}
	}
}

// The role names the server reads are the enum's: every role of the enum is a core role, so the
// field a handoff must fill is found by the role's name and no table.
func TestEveryRoleOfTheEnumIsACoreRoleOfTheSameName(t *testing.T) {
	for i := 1; i < pb.AgentRole(0).Descriptor().Values().Len(); i++ {
		role := pb.AgentRole(pb.AgentRole(0).Descriptor().Values().Get(i).Number())
		if !slices.Contains(roles, core.AgentRole(names.Spell(role))) {
			t.Errorf("%v is spelled %q, which is no core role", role, names.Spell(role))
		}
	}
}

// Every role that may post a handoff has a body for it, spelled as the role.
func TestEverySubagentRoleHasAHandoffBody(t *testing.T) {
	body := (&pb.Handoff{}).ProtoReflect().Descriptor().Oneofs().ByName("body")
	for _, role := range roles {
		if !core.Allowed(core.Principal(role), core.ActionPostHandoff) {
			continue
		}
		if body.Fields().ByName(protoreflect.Name(role)) == nil {
			t.Errorf("a %s may post a handoff and Handoff has no body for it", role)
		}
	}
}

func TestListAgentsSaysRoleAndStatusInPlainWords(t *testing.T) {
	var agents []*pb.Agent
	statuses := pb.AgentStatus(0).Descriptor().Values()
	for i := 1; i < statuses.Len(); i++ {
		agents = append(agents, &pb.Agent{Id: fmt.Sprintf("a-%d", i), RunId: "r-1", Role: pb.AgentRole_AGENT_ROLE_WORKER, Status: pb.AgentStatus(statuses.Get(i).Number()), PlanNumber: 2, ParentAgentId: "a-1"})
	}
	agents = append(agents, &pb.Agent{Id: "a-1", RunId: "r-1", Role: pb.AgentRole_AGENT_ROLE_LEAD, Status: pb.AgentStatus_AGENT_STATUS_WAITING})
	text, isErr := call(t, newServer(core.RoleLead, &fakeDaemon{agents: agents}), 1, "list_agents", map[string]any{})
	if isErr {
		t.Fatal(text)
	}
	for _, raw := range []string{"AGENT_STATUS", "AGENT_ROLE", "_IDLE", "<no value>", "{{", "map["} {
		if strings.Contains(text, raw) {
			t.Errorf("the list says %q: %s", raw, text)
		}
	}
	if strings.Contains(text, "unknown state") {
		t.Errorf("a status has no word: %s", text)
	}
	for _, want := range []string{"a-1 (you): lead", "worker, plan 2, working on a turn, started by a-1", "idle, between turns"} {
		if !strings.Contains(text, want) {
			t.Errorf("the list does not say %q: %s", want, text)
		}
	}
	empty, _ := call(t, newServer(core.RoleLead, &fakeDaemon{}), 2, "list_agents", map[string]any{})
	if !strings.Contains(empty, "no agents") {
		t.Errorf("an empty run is said as %q", empty)
	}
}

func TestEveryDeliveryIsSaidInWords(t *testing.T) {
	deliveries := pb.Delivery(0).Descriptor().Values()
	for i := 1; i < deliveries.Len(); i++ {
		d := &fakeDaemon{receipt: &pb.Receipt{MessageId: "m-9", Delivery: pb.Delivery(deliveries.Get(i).Number())}}
		text, isErr := call(t, newServer(core.RoleLead, d), 1, "send_message", map[string]any{"agent_id": "a-2", "text": "hi"})
		if isErr || !strings.HasPrefix(text, "message m-9 is ") || strings.Contains(text, "on its way") && deliveries.Get(i).Name() != "DELIVERY_CONSUMED" || strings.Contains(text, "DELIVERY_") {
			t.Errorf("%s is said as %q", deliveries.Get(i).Name(), text)
		}
	}
}
