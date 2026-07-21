package trainticket

import "testing"

func TestParseConfigRejectsUnknownMalformedAndSmallValues(t *testing.T) {
	for _, values := range []map[string]any{
		{"recrods": int64(32)},
		{"records": "32"},
		{"records": int64(1)},
		{"records": 2.5},
	} {
		if _, err := ParseConfig(values); err == nil {
			t.Fatalf("accepted malformed config %v", values)
		}
	}
	config, err := ParseConfig(map[string]any{"records": int64(8)})
	if err != nil || config.Records != 8 {
		t.Fatalf("config=%+v error=%v", config, err)
	}
}
