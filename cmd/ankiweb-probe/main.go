// Command ankiweb-probe exercises the AnkiWeb client from the command line.
// Read-only unless -answer is given.
//
//	ANKIWEB_USERNAME=... ANKIWEB_PASSWORD=... go run ./cmd/ankiweb-probe -deck History
//	ANKIWEB_USERNAME=... ANKIWEB_PASSWORD=... go run ./cmd/ankiweb-probe -answer 3   # WRITES a review
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/amarcozzi/anki-mcp/internal/ankiweb"
)

func main() {
	deck := flag.String("deck", "", "select this deck (full name) before fetching")
	answer := flag.Int("answer", 0, "grade the shown card 1-4 (WRITES a review)")
	raw := flag.Bool("raw", false, "also print the raw question/answer HTML")
	flag.Parse()

	user, pw := os.Getenv("ANKIWEB_USERNAME"), os.Getenv("ANKIWEB_PASSWORD")
	if user == "" || pw == "" {
		fmt.Fprintln(os.Stderr, "set ANKIWEB_USERNAME and ANKIWEB_PASSWORD")
		os.Exit(2)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	c := ankiweb.New(user, pw)

	if err := c.Login(ctx); err != nil {
		die(err)
	}
	fmt.Println("login: ok")

	decks, err := c.Decks(ctx)
	if err != nil {
		die(err)
	}
	var deckID int64
	for _, d := range decks {
		mark := " "
		if d.Current {
			mark = "*"
		}
		fmt.Printf("%s %s%-45s new=%-4d learn=%-4d review=%d\n", mark, strings.Repeat("  ", d.Level), d.Name, d.New, d.Learn, d.Review)
		if *deck != "" && strings.EqualFold(d.Name, *deck) {
			deckID = d.ID
		}
	}
	if *deck != "" {
		if deckID == 0 {
			die(fmt.Errorf("no deck named %q", *deck))
		}
		if err := c.SelectDeck(ctx, deckID); err != nil {
			die(err)
		}
		fmt.Println("selected deck:", *deck)
	}

	q, err := c.Next(ctx)
	if err != nil {
		die(err)
	}
	fmt.Printf("\nqueue: new=%d learn=%d review=%d, cards returned=%d\n", q.New, q.Learn, q.Review, len(q.Cards))
	if len(q.Cards) == 0 {
		fmt.Println("nothing due")
		return
	}
	card := q.Cards[0]
	front := ankiweb.Render(card.Question)
	back := ankiweb.RenderBack(card.Question, card.Answer)
	fmt.Printf("\ncard %d (note %d)\nFRONT: %s\nBACK : %s\nbuttons: %v\nimages: %v\n",
		card.CardId, card.NoteId, front.Text, back.Text, card.ButtonLabels, append(front.Images, back.Images...))

	if *raw {
		fmt.Printf("\nRAW QUESTION:\n%s\n\nRAW ANSWER:\n%s\n", card.Question, card.Answer)
	}

	if *answer > 0 {
		next, err := c.Answer(ctx, card.CardId, ankiweb.Rating(*answer), 5*time.Second)
		if err != nil {
			die(err)
		}
		fmt.Printf("\nanswered card %d with %d; queue now new=%d learn=%d review=%d\n", card.CardId, *answer, next.New, next.Learn, next.Review)
		if len(next.Cards) > 0 {
			fmt.Printf("next card %d: %s\n", next.Cards[0].CardId, ankiweb.Render(next.Cards[0].Question).Text)
		}
	}
}

func die(err error) {
	fmt.Fprintln(os.Stderr, "error:", err)
	os.Exit(1)
}
