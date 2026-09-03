package ankiweb

import "testing"

func TestRender(t *testing.T) {
	in := `<style>body{color:red}</style><div class="front">What is <b>1 volt</b>?<br><img src="v.png"> HINT:&nbsp;\(1.6 \times 10^{-19}\)C</div>[sound:a.mp3]`
	got := Render(in)
	want := "What is 1 volt?\n[image: v.png] HINT: \\(1.6 \\times 10^{-19}\\)C\n[audio: a.mp3]"
	if got.Text != want {
		t.Errorf("Text = %q, want %q", got.Text, want)
	}
	if len(got.Images) != 1 || got.Images[0] != "v.png" || len(got.Sounds) != 1 {
		t.Errorf("media = %v %v", got.Images, got.Sounds)
	}
}

func TestRenderBack(t *testing.T) {
	cases := []struct{ name, q, a, want string }{
		{"marker", `<div>front</div>`, `<div>front</div><hr id=answer><div>back</div>`, "back"},
		{"prefix", `<div class="q"><div>Ctx</div> Question?</div>`,
			`<div class="q"><div>Ctx</div> Question?</div><div class="a"><hr>Answer<img src="a.png"></div>`, "Answer [image: a.png]"},
		{"no overlap", `<div>front</div>`, `<div>only back</div>`, "only back"},
	}
	for _, c := range cases {
		got := RenderBack(c.q, c.a)
		if got.Text != c.want {
			t.Errorf("%s: got %q want %q", c.name, got.Text, c.want)
		}
		if c.name == "prefix" && (len(got.Images) != 1 || got.Images[0] != "a.png") {
			t.Errorf("prefix images = %v", got.Images)
		}
	}
}
