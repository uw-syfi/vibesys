package main

import "testing"

func TestParseCommandJSONRejectsMalformedAndEmptyArguments(t *testing.T) {
	for _, raw := range []string{
		`"./run.sh"`,
		`[]`,
		`["./run.sh",""]`,
		`["./run.sh",1]`,
	} {
		if _, err := parseCommandJSON(raw); err == nil {
			t.Fatalf("accepted invalid command %s", raw)
		}
	}
	command, err := parseCommandJSON(`["./run.sh","--port","8080"]`)
	if err != nil {
		t.Fatal(err)
	}
	if len(command) != 3 || command[0] != "./run.sh" {
		t.Fatalf("command=%v", command)
	}
}
