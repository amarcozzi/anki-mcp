// Package tools defines the MCP tools exposed to the chat agent.
//
// The server is stateless: every tool re-derives "the current card" from
// AnkiWeb, and answer/reveal refuse to act on a card that is not current.
package tools

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/amarcozzi/anki-mcp/internal/ankiweb"
)

// Tools holds the dependencies the tool handlers need.
type Tools struct {
	anki *ankiweb.Client
}

// New constructs the tool set.
func New(anki *ankiweb.Client) *Tools { return &Tools{anki: anki} }

// Register adds all tools to server.
func (t *Tools) Register(server *mcp.Server) {
	mcp.AddTool(server, &mcp.Tool{
		Name:        "list_decks",
		Description: "List Anki decks with their due counts (new, learning, review) and which deck is currently selected.",
	}, t.ListDecks)
	mcp.AddTool(server, &mcp.Tool{
		Name: "next_card",
		Description: "Get the next card to review from the current deck (or select a deck by name first). " +
			"Returns the FRONT only. Show it to the user, let them answer, then call reveal.",
	}, t.NextCard)
	mcp.AddTool(server, &mcp.Tool{
		Name: "reveal",
		Description: "Reveal the BACK of the current card so you can compare it with the user's answer. " +
			"Returns the four rating options with their next intervals. Propose a rating; let the user confirm unless they asked you to auto-grade.",
	}, t.Reveal)
	mcp.AddTool(server, &mcp.Tool{
		Name: "answer",
		Description: "Grade the current card (1=Again, 2=Hard, 3=Good, 4=Easy). This writes a review to the user's Anki collection " +
			"and returns the next card's front. Cannot be undone from here.",
	}, t.Answer)
}

type ListDecksInput struct{}

func (t *Tools) ListDecks(ctx context.Context, _ *mcp.CallToolRequest, _ ListDecksInput) (*mcp.CallToolResult, any, error) {
	decks, err := t.anki.Decks(ctx)
	if err != nil {
		return nil, nil, err
	}
	var b strings.Builder
	for _, d := range decks {
		mark := " "
		if d.Current {
			mark = "*"
		}
		fmt.Fprintf(&b, "%s %s%s  (new %d, learn %d, review %d)\n", mark, strings.Repeat("  ", d.Level), d.Name, d.New, d.Learn, d.Review)
	}
	b.WriteString("\n* = currently selected deck")
	return text(b.String()), nil, nil
}

type NextCardInput struct {
	Deck string `json:"deck,omitempty" jsonschema:"Full deck name to select first, such as Fire Science::Fire Dynamics. Omit to keep the current deck."`
}

func (t *Tools) NextCard(ctx context.Context, _ *mcp.CallToolRequest, in NextCardInput) (*mcp.CallToolResult, any, error) {
	if in.Deck != "" {
		decks, err := t.anki.Decks(ctx)
		if err != nil {
			return nil, nil, err
		}
		id, ok := findDeck(decks, in.Deck)
		if !ok {
			return errText("no deck named %q; call list_decks", in.Deck), nil, nil
		}
		if err := t.anki.SelectDeck(ctx, id); err != nil {
			return nil, nil, err
		}
	}
	q, err := t.anki.Next(ctx)
	if err != nil {
		return nil, nil, err
	}
	return t.front(ctx, q), nil, nil
}

type RevealInput struct {
	CardID int64 `json:"card_id" jsonschema:"The card_id returned by next_card or answer."`
}

func (t *Tools) Reveal(ctx context.Context, _ *mcp.CallToolRequest, in RevealInput) (*mcp.CallToolResult, any, error) {
	q, err := t.anki.Next(ctx)
	if err != nil {
		return nil, nil, err
	}
	if len(q.Cards) == 0 || q.Cards[0].CardId != in.CardID {
		return errText("card %d is no longer the current card; call next_card", in.CardID), nil, nil
	}
	card := q.Cards[0]
	back := ankiweb.RenderBack(card.Question, card.Answer)
	var b strings.Builder
	fmt.Fprintf(&b, "card_id: %d\n\nBACK:\n%s\n\nRatings:\n", card.CardId, back.Text)
	for i, label := range card.ButtonLabels {
		fmt.Fprintf(&b, "  %d = %-5s (next: %s)\n", i+1, []string{"Again", "Hard", "Good", "Easy"}[i], cleanLabel(label))
	}
	res := text(b.String())
	t.attachImages(ctx, res, back.Images)
	return res, nil, nil
}

type AnswerInput struct {
	CardID       int64 `json:"card_id" jsonschema:"The card being graded; must be the current card."`
	Rating       int   `json:"rating" jsonschema:"Rating: 1 for Again, 2 for Hard, 3 for Good, 4 for Easy"`
	SecondsTaken int   `json:"seconds_taken,omitempty" jsonschema:"Roughly how long the user took, for Anki's statistics. Default 15."`
}

func (t *Tools) Answer(ctx context.Context, _ *mcp.CallToolRequest, in AnswerInput) (*mcp.CallToolResult, any, error) {
	if in.Rating < 1 || in.Rating > 4 {
		return errText("rating must be 1-4"), nil, nil
	}
	took := 15 * time.Second
	if in.SecondsTaken > 0 {
		took = time.Duration(in.SecondsTaken) * time.Second
	}
	q, err := t.anki.Answer(ctx, in.CardID, ankiweb.Rating(in.Rating), took)
	if err == ankiweb.ErrNotCurrent {
		return errText("card %d is no longer the current card; nothing was graded. Call next_card.", in.CardID), nil, nil
	}
	if err != nil {
		return nil, nil, err
	}
	res := t.front(ctx, q)
	res.Content = append([]mcp.Content{&mcp.TextContent{Text: fmt.Sprintf("Graded card %d as %d.\n\n", in.CardID, in.Rating)}}, res.Content...)
	return res, nil, nil
}

// front renders the queue head as a tool result.
func (t *Tools) front(ctx context.Context, q *ankiweb.Queue) *mcp.CallToolResult {
	if len(q.Cards) == 0 {
		return text("No cards due in the current deck. Congratulations. Call list_decks to pick another deck.")
	}
	card := q.Cards[0]
	fr := ankiweb.Render(card.Question)
	var b strings.Builder
	fmt.Fprintf(&b, "card_id: %d\nremaining: new %d, learn %d, review %d\n\nFRONT:\n%s", card.CardId, q.New, q.Learn, q.Review, fr.Text)
	if len(fr.Images) > 0 && strings.TrimSpace(strings.NewReplacer("[image:", "", "]", "").Replace(fr.Text)) == "" {
		b.WriteString("\n\n(Front is image-only. The user cannot see it in chat. Describe it only if the user asks; otherwise suggest skipping by grading it later on the desktop.)")
	}
	res := text(b.String())
	t.attachImages(ctx, res, fr.Images)
	return res
}

// attachImages fetches up to two images and adds them as image content so the
// model can see them. Failures are non-fatal: the text already names the file.
func (t *Tools) attachImages(ctx context.Context, res *mcp.CallToolResult, names []string) {
	for i, name := range names {
		if i >= 2 {
			break
		}
		data, mime, err := t.anki.Media(ctx, name)
		if err != nil || !strings.HasPrefix(mime, "image/") {
			continue
		}
		res.Content = append(res.Content, &mcp.ImageContent{Data: data, MIMEType: mime})
	}
}

func findDeck(decks []ankiweb.Deck, name string) (int64, bool) {
	for _, d := range decks {
		if strings.EqualFold(d.Name, name) {
			return d.ID, true
		}
	}
	// fall back to a unique subdeck match, so "Fire Dynamics" finds "Fire Science::Fire Dynamics"
	var hit []ankiweb.Deck
	for _, d := range decks {
		if strings.HasSuffix(strings.ToLower(d.Name), "::"+strings.ToLower(name)) {
			hit = append(hit, d)
		}
	}
	if len(hit) == 1 {
		return hit[0].ID, true
	}
	return 0, false
}

// cleanLabel strips the Unicode isolate marks Anki wraps around intervals.
func cleanLabel(s string) string {
	return strings.NewReplacer("⁨", "", "⁩", "").Replace(s)
}

func text(s string) *mcp.CallToolResult {
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: s}}}
}

func errText(format string, args ...any) *mcp.CallToolResult {
	return &mcp.CallToolResult{IsError: true, Content: []mcp.Content{&mcp.TextContent{Text: fmt.Sprintf(format, args...)}}}
}
