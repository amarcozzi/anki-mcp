package ankiweb

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"sync"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/amarcozzi/anki-mcp/internal/ankiweb/ankiwebpb"
)

const (
	webBase  = "https://ankiweb.net"
	userBase = "https://ankiuser.net" // study/edit pages and their RPCs live here
	mediaMax = 4 << 20                // refuse media files larger than this
)

// ErrNotCurrent is returned when the caller tries to act on a card that is no
// longer the card AnkiWeb considers current.
var ErrNotCurrent = errors.New("card is no longer the current card")

// Rating is an answer button: 1 Again, 2 Hard, 3 Good, 4 Easy.
type Rating uint32

// Client talks to AnkiWeb as the browser study page would. It is safe for
// concurrent use, but calls are serialized: AnkiWeb keeps one review queue per
// account and interleaved requests would confuse it.
type Client struct {
	http     *http.Client
	username string
	password string
	mu       sync.Mutex
	loggedIn bool
}

// New returns a client that will log in lazily on first use.
func New(username, password string) *Client {
	jar, _ := cookiejar.New(nil)
	return &Client{
		http:     &http.Client{Jar: jar, Timeout: 30 * time.Second},
		username: username,
		password: password,
	}
}

// Login authenticates against ankiweb.net and hands the session to ankiuser.net.
func (c *Client) Login(ctx context.Context) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.login(ctx)
}

func (c *Client) login(ctx context.Context) error {
	var out pb.LoginResponse
	if err := c.rpc(ctx, webBase+"/svc/account/login",
		&pb.LoginRequest{Username: c.username, Password: c.password}, &out); err != nil {
		return fmt.Errorf("ankiweb login: %w", err)
	}
	if out.Status != pb.LoginStatus_LOGIN_AUTHENTICATED {
		return fmt.Errorf("ankiweb login rejected: %s", out.Status)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		userBase+"/account/ankiuser-login?t="+url.QueryEscape(out.Token), nil)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("ankiuser handoff: %w", err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("ankiuser handoff: HTTP %d", resp.StatusCode)
	}
	c.loggedIn = true
	return nil
}

// rpc posts a protobuf message and decodes the protobuf reply. No auth handling.
func (c *Client) rpc(ctx context.Context, endpoint string, in, out proto.Message) error {
	body, err := proto.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("User-Agent", "anki-mcp/0.1 (+https://github.com/amarcozzi/anki-mcp)")
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode == http.StatusForbidden {
		return errForbidden
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: HTTP %d: %.200s", endpoint, resp.StatusCode, raw)
	}
	return proto.Unmarshal(raw, out)
}

var errForbidden = errors.New("forbidden")

// call is rpc with lazy login and one re-login on 403 (expired cookie).
// Callers must hold c.mu.
func (c *Client) call(ctx context.Context, endpoint string, in, out proto.Message) error {
	if !c.loggedIn {
		if err := c.login(ctx); err != nil {
			return err
		}
	}
	err := c.rpc(ctx, endpoint, in, out)
	if errors.Is(err, errForbidden) {
		c.loggedIn = false
		if err := c.login(ctx); err != nil {
			return err
		}
		err = c.rpc(ctx, endpoint, in, out)
	}
	return err
}

// Deck is a flattened entry of the deck tree.
type Deck struct {
	ID      int64
	Name    string // full name, "Parent::Child"
	Level   int
	New     uint32
	Learn   uint32
	Review  uint32
	Current bool
}

// Decks returns every deck with its due counts, in tree order.
func (c *Client) Decks(ctx context.Context) ([]Deck, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, offset := time.Now().Zone()
	var out pb.DeckListResponse
	if err := c.call(ctx, webBase+"/svc/decks/deck-list-info",
		&pb.DeckListRequest{MinutesWestOfUtc: proto.Int32(int32(-offset / 60))}, &out); err != nil {
		return nil, err
	}
	var decks []Deck
	var walk func(n *pb.DeckNode, prefix string, level int)
	walk = func(n *pb.DeckNode, prefix string, level int) {
		name := n.Name
		if prefix != "" {
			name = prefix + "::" + n.Name
		}
		if level > 0 { // level 0 is the synthetic root
			decks = append(decks, Deck{ID: n.DeckId, Name: name, Level: level - 1,
				New: n.NewCount, Learn: n.LearnCount, Review: n.ReviewCount, Current: n.DeckId == out.CurrentDeckId})
		} else {
			name = ""
		}
		for _, ch := range n.Children {
			walk(ch, name, level+1)
		}
	}
	if out.TopNode != nil {
		walk(out.TopNode, "", 0)
	}
	return decks, nil
}

// SelectDeck makes deckID the current deck, the one study-cards draws from.
func (c *Client) SelectDeck(ctx context.Context, deckID int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.call(ctx, webBase+"/svc/decks/select-deck", &pb.SelectDeckRequest{DeckId: deckID}, &pb.SelectDeckResponse{})
}

// Queue is what AnkiWeb returns from study-cards: the next card(s) and counts.
type Queue struct {
	Cards  []*pb.Card // Cards[0] is the current card; may be empty when done
	New    uint32
	Learn  uint32
	Review uint32
}

// Next fetches the current card without answering anything. Idempotent.
func (c *Client) Next(ctx context.Context) (*Queue, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.study(ctx, &pb.StudyCardsRequest{})
}

// Answer grades cardID, which must still be the current card, and returns the
// queue that follows. took is how long the review took; it only feeds stats.
func (c *Client) Answer(ctx context.Context, cardID int64, r Rating, took time.Duration) (*Queue, error) {
	if r < 1 || r > 4 {
		return nil, fmt.Errorf("rating must be 1-4, got %d", r)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	cur, err := c.study(ctx, &pb.StudyCardsRequest{})
	if err != nil {
		return nil, err
	}
	if len(cur.Cards) == 0 || cur.Cards[0].CardId != cardID {
		return cur, ErrNotCurrent
	}
	card := cur.Cards[0]
	ns := card.NextStates
	if ns == nil {
		return nil, errors.New("ankiweb returned a card without scheduling states")
	}
	next := map[Rating][]byte{1: ns.Again, 2: ns.Hard, 3: ns.Good, 4: ns.Easy}[r]
	req := &pb.StudyCardsRequest{Answer: &pb.CardAnswer{
		CardId:           cardID,
		AnswerButton:     uint32(r),
		TimeTakenMillis:  uint32(min(took.Milliseconds(), 60_000)),
		AnsweredAtMillis: time.Now().UnixMilli(),
		CurrentState:     ns.Current,
		NextState:        next,
	}}
	// Deliberately not setting NextCardId: when the client names the card it is
	// about to show, AnkiWeb returns that card with empty question/answer on the
	// assumption the client already has it. We want full content every time.
	return c.study(ctx, req)
}

func (c *Client) study(ctx context.Context, req *pb.StudyCardsRequest) (*Queue, error) {
	var out pb.StudyCardsResponse
	if err := c.call(ctx, userBase+"/svc/study/study-cards", req, &out); err != nil {
		return nil, err
	}
	if out.SchedVer != 0 && out.SchedVer < 3 {
		return nil, fmt.Errorf("ankiweb requires the V3 scheduler (collection has v%d)", out.SchedVer)
	}
	return &Queue{Cards: out.Cards, New: out.NewCount, Learn: out.LearnCount, Review: out.ReviewCount}, nil
}

// Media downloads one media file referenced by a card, returning its bytes and MIME type.
func (c *Client) Media(ctx context.Context, filename string) ([]byte, string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.loggedIn {
		if err := c.login(ctx); err != nil {
			return nil, "", err
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, userBase+"/study/media/"+url.PathEscape(filename), nil)
	if err != nil {
		return nil, "", err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, "", fmt.Errorf("media %q: HTTP %d", filename, resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, mediaMax+1))
	if err != nil {
		return nil, "", err
	}
	if len(data) > mediaMax {
		return nil, "", fmt.Errorf("media %q exceeds %d bytes", filename, mediaMax)
	}
	mime := resp.Header.Get("Content-Type")
	if mime == "" {
		mime = http.DetectContentType(data)
	}
	return data, mime, nil
}

// StudyRaw sends an arbitrary study-cards request. Exposed for experiments.
func (c *Client) StudyRaw(ctx context.Context, req *pb.StudyCardsRequest) (*Queue, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.study(ctx, req)
}
