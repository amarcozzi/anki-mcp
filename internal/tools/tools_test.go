package tools

import (
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/amarcozzi/anki-mcp/internal/ankiweb"
)

// Register panics if a tool's input struct has a bad jsonschema tag, so make
// sure that happens under `go test` rather than at container start.
func TestRegister(t *testing.T) {
	s := mcp.NewServer(&mcp.Implementation{Name: "t", Version: "0"}, nil)
	New(ankiweb.New("u", "p")).Register(s)
}

func TestFindDeck(t *testing.T) {
	decks := []ankiweb.Deck{{ID: 1, Name: "Fire Science"}, {ID: 2, Name: "Fire Science::Fire Dynamics"}, {ID: 3, Name: "History"}}
	for _, tc := range []struct {
		q    string
		want int64
		ok   bool
	}{{"history", 3, true}, {"Fire Dynamics", 2, true}, {"Fire Science", 1, true}, {"Science", 0, false}, {"nope", 0, false}} {
		got, ok := findDeck(decks, tc.q)
		if got != tc.want || ok != tc.ok {
			t.Errorf("findDeck(%q) = %d,%v want %d,%v", tc.q, got, ok, tc.want, tc.ok)
		}
	}
}
