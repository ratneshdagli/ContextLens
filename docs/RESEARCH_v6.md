# Research v6 — a truly live voice, why retrieval lied once, and what makes people pay for news

Follow-up to [RESEARCH_v5.md](RESEARCH_v5.md), triggered by three things the user reported after using v5 for real:
a "Talk to Lens" answer that cited the wrong story, voice going silent during a background refresh, and a general
"this is 10% of what I imagined" push for more depth. Two research passes (news-reader pain points + paid-subscription
drivers; live-voice barge-in architecture, plus a check on "Jev AI") fed the fixes and features below.

## What was actually broken (found by reading the code, not guessing)

1. **Wrong-story answers.** `_retrieve()` picked the top story by embedding cosine alone. Two stories that share only
   generic vocabulary — "FIR", "case", "probe" — can sit at 0.6–0.7 cosine even when they are about entirely
   different people. Asking about a comedian's FIR surfaced an unrelated actor's-death story; the model then spoke
   confidently from the wrong context. **Fix:** a candidate that shares no real content word with the question is now
   dropped unless the raw cosine is overwhelming (≥0.80). A second gap compounded it — the story's *card* was shown
   before we knew whether any sentence about it would survive grounding, so a rejected answer still displayed a
   confusing, unrelated card. Cards are now held back until the first grounded sentence actually exists.
2. **Voice going silent during a refresh.** Every LLM call is serialised through one gate (a 6 GB GPU can only run one
   model at a time), with a priority flag so a live reader jumps the background pipeline queue — but `voice.py` never
   set that flag. A spoken question queued *behind* whatever the scheduled refresh was doing. Fixed with one call
   (`llm.interactive()`) at the top of every voice turn, plus a `queued` status phase so you can see when it's
   waiting on the GPU rather than wondering if it's broken.
3. **No memory, no visible progress.** The conversation lived only in React state, gone on navigating away; and the
   only feedback during a slow answer was a plain status line. Fixed with localStorage-backed conversation memory
   (auto-expires after 6 hours) and a small phase-tagged status pill (thinking / queued / reading coverage /
   answering / digging).

## Live voice: real barge-in, not a checkbox

Chrome's `SpeechRecognition` cannot take its own audio-constraint stream, so running it *while* `speechSynthesis` is
talking mostly just means the mic hears Lens's own voice — production voice agents (LiveKit, Pipecat, vocode) solve
this server-side with a dedicated VAD/turn-detection model on a full-duplex audio pipeline, which a browser-only app
doesn't have. The workable browser-only recipe: open a **second**, dedicated echo-cancelled mic stream used only to
watch energy, learn the ambient/TTS-bleed floor while Lens is talking, and treat a *sustained* run of frames clearly
above that floor (not a single spike) as "the reader is talking" — then cancel speech synthesis and start real
recognition from that point, rather than trying to transcribe over the TTS at all. That's what v6 ships, on by
default; headphones remove the bleed problem entirely and make it near-instant.

**"Jev AI"** (asked about directly): it's a real product — a fast non-autoregressive classifier model from a
2026 startup — but it's a paid cloud API for structured decisions, not a voice or audio tool, and not self-hostable.
It doesn't fit here; nothing in this app needed it.

## What's actually wrong with news apps, and why people pay anyway

Beyond the news-avoidance numbers already in v5: the sharpest 2025–2026 complaints are **verification collapse**
during breaking news (AI content farms and confident-sounding false claims flood a story before it's confirmed —
exactly where this app's grounding checks earn their keep), **paywall bait-and-switch** (a story teased in-feed
with no warning it's locked), **algorithmic repetition** (the same story surfacing 5–8 times), and **no follow-up**
weeks later on a story you cared about. Meanwhile, what people actually pay for is rarely "more news": NYT's
bundle subscribers (Games, Cooking, Wirecutter) retain far better than news-only ones; Ground News' paying feature
is a bias/coverage-comparison *tool*, not exclusive reporting; The Athletic and The Ken won on narrow, deep, ad-free
writing over broad shallow coverage. The throughline for a local, non-commercial app: **tool value beats scarcity**.

## What shipped from this

- **Personal digest** (new `/digest` page): track a GitHub repo (real commits, LLM-summarised, never invented) or a
  topic — typed, or pulled from an uploaded document — matched against this app's *own* aggregated stories by
  embedding search, so a topic digest is render-only and can't hallucinate. This is the "what's new in my docs and
  repos" ask, and it sits next to the pre-existing story-follow feature (`/api/following`) rather than duplicating it.
- **Retrieval + card-display fixes** above, with regression tests reproducing the exact failure.
- **`llm.interactive()` in voice.py**, with a `queued` status phase, so a live conversation never silently waits out
  a scheduled refresh.
- **Real photos in the voice UI** — story cards spoken about in conversation now show the same image already stored
  for that story on the Today page, instead of a plain text tile.
- **True(r) barge-in**, on by default, per the recipe above.
- **Conversation memory** across page loads/navigation, with a visible "picked up your last conversation" note.

## Honest limits carried forward

Barge-in is best-effort energy detection, not a trained turn-detector — it can still mis-fire on a loud cough near
a laptop's built-in mic, and headphones are the real fix. A repo digest is capped at commits from the last 7 days
via GitHub's unauthenticated API (60 requests/hour), so a private repo or a very high check frequency isn't
supported. A topic digest only ever lists stories already in this app's own database — it doesn't call an LLM, so it
can miss a real development that simply wasn't in this week's ~230 feeds.
