package ankiweb

import (
	"html"
	"regexp"
	"strings"
)

var (
	reStyle  = regexp.MustCompile(`(?is)<style.*?</style>`)
	reScript = regexp.MustCompile(`(?is)<script.*?</script>`)
	reImg    = regexp.MustCompile(`(?i)<img[^>]*?src="([^"]+)"[^>]*>`)
	reSound  = regexp.MustCompile(`\[sound:([^\]]+)\]`)
	reBreak  = regexp.MustCompile(`(?i)</?(br|p|div|li|ul|ol|tr|table|hr|h[1-6]|blockquote|pre)\b[^>]*>`)
	reCell   = regexp.MustCompile(`(?i)</?(td|th)\b[^>]*>`)
	reTag    = regexp.MustCompile(`<[^>]+>`)
	reSpaces = regexp.MustCompile(`[ \t\x{00a0}]+`)
	reLines  = regexp.MustCompile(`\n{3,}`)
	reAnswer = regexp.MustCompile(`(?is)<hr id="?answer"?[^>]*>`)
)

// Rendered is a card side reduced to what a chat agent can use.
type Rendered struct {
	Text   string   // plain text; images replaced by "[image: name]", MathJax kept as LaTeX
	Images []string // media filenames referenced by <img>, in order
	Sounds []string // media filenames referenced by [sound:...]
}

// Render converts card HTML to plain text.
func Render(h string) Rendered {
	var r Rendered
	h = reStyle.ReplaceAllString(h, "")
	h = reScript.ReplaceAllString(h, "")
	h = reImg.ReplaceAllStringFunc(h, func(m string) string {
		src := html.UnescapeString(reImg.FindStringSubmatch(m)[1])
		r.Images = append(r.Images, src)
		return " [image: " + src + "] "
	})
	h = reSound.ReplaceAllStringFunc(h, func(m string) string {
		name := reSound.FindStringSubmatch(m)[1]
		r.Sounds = append(r.Sounds, name)
		return " [audio: " + name + "] "
	})
	h = reBreak.ReplaceAllString(h, "\n")
	h = reCell.ReplaceAllString(h, " ")
	h = reTag.ReplaceAllString(h, "")
	h = html.UnescapeString(h)
	var lines []string
	for _, line := range strings.Split(h, "\n") {
		line = strings.TrimSpace(reSpaces.ReplaceAllString(line, " "))
		lines = append(lines, line)
	}
	r.Text = strings.TrimSpace(reLines.ReplaceAllString(strings.Join(lines, "\n"), "\n\n"))
	return r
}

// RenderBack renders the answer side with the repeated question removed.
// Most templates start the back with {{FrontSide}}; some mark the boundary
// with <hr id=answer>, others with a bare <hr> or nothing at all. So: use the
// marker when present, otherwise strip the rendered front as a text prefix.
func RenderBack(questionHTML, answerHTML string) Rendered {
	if loc := reAnswer.FindStringIndex(answerHTML); loc != nil {
		return Render(answerHTML[loc[1]:])
	}
	front := Render(questionHTML)
	back := Render(answerHTML)
	if front.Text != "" && strings.HasPrefix(back.Text, front.Text) {
		back.Text = strings.TrimSpace(strings.TrimPrefix(back.Text, front.Text))
		back.Images = back.Images[min(len(front.Images), len(back.Images)):]
		back.Sounds = back.Sounds[min(len(front.Sounds), len(back.Sounds)):]
	}
	return back
}
