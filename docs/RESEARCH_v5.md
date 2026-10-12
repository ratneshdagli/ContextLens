# ContextLens v5 — what readers struggle with, and what we built for it

This is the research behind the v5 pass (Sept 2026). Numbers are from the sources linked at the end; where a claim is *my reading* of a
source rather than a figure the source states, it is marked. I did not run a survey of my own.

## 1. The problems people actually report

| # | Problem | Evidence | What ContextLens does |
|---|---|---|---|
| 1 | **"Too hard to understand"** — stories assume you know the backstory | Coverage of the Reuters Institute *Digital News Report 2026* says under-35s are far more likely than older groups to call news too hard to understand. Practitioner pieces on Gen Z news habits repeat the point that stories assume the backstory (my reading of the sources below). | Every story has a plain-language *why-chain* (oldest cause first, each step tied to its source sentence), a glossary of jargon, a timeline, and a "go deeper" row. **Lens** (voice) explains out loud. |
| 2 | **News avoidance** — people switch off | 42% of people worldwide sometimes/often avoid news, up from 29% in 2017 (DNR 2026). In India 52% (The Wire's summary of DNR 2026). | A finite front page ("you're caught up"), time budgets ("I have 5 minutes"), Flow mode, and **Calm mode** (below). |
| 3 | **Feeling powerless / mood** — heavy news wears people down | Per coverage of DNR 2026, younger respondents more often say they feel powerless about issues like economic insecurity and climate, that news is not relevant to them, or that it leads to arguments. | **Calm mode** hides graphic and distressing stories from Today (they stay in Everything). Every explainer ends with *who is affected* so a story lands as "what does it mean for me", not just dread. |
| 4 | **Repetition** — the same story ten times | Reasons for avoidance include content that feels repetitive (DNR 2026 coverage). | **Merged stories**: a reaction, a follow-up and a profile of one event are now *one* story with "other angles" listed inside it. |
| 5 | **Where young people get news is not newsrooms** | 52% of 18–24s say social, video or AI is their main way to get news (DNR 2026). In India 58% use YouTube and 56% WhatsApp for news (Newslaundry/TNM summaries of DNR 2026). | A short-form **Flow** deck, a **voice** assistant, and a **Share on WhatsApp** button that sends the gist, the reasons and the source link. |
| 6 | **AI chatbots are used for follow-up questions — and not fully trusted** | 10% use AI chatbots for news weekly (16% of under-35s). The top use, at 42%, is asking a follow-up question. Only 20% trust chatbot answers (DNR 2026). | **Lens** answers from stories the app already explained, checks every spoken sentence against its sources *before* saying it, and shows its sources. It says "I don't have a reliable answer" instead of bluffing. |
| 7 | **Trust and bias** | Creators are seen as easier to understand but less trustworthy and impartial; brand trust is low (DNR 2026). | **Sources** page (91 outlets, trust tiers, live counts), **fact-checkers** read as newsrooms (Alt News, BOOM, Factly, Newschecker, Vishvas News) with a *Fact-check* tag and filter, and forensic analysis of any selected text. |
| 8 | **Coverage gaps** — important stories missing | (Our own finding: 61 feeds, mostly one section per outlet, no regional press, no primary sources.) | 224 live feeds from 91 outlets; **Everything** page lists every story from the last 3 days, not only the ranked ones. |

## 2. What was built

1. **Duplicate stories merged.** Clusters that formed independently are compared again after every ingest. The rule is deliberately strict —
   centroid similarity ≥0.96, or ≥0.89 with one *rare* shared name, or ≥0.86 with two — and a survivor only absorbs stories that match *it*
   (no chaining), so unrelated stories cannot snowball. Also fixed: `IIT-Bombay` and `IIT Bombay` were different tokens. On the live data:
   27 duplicate stories merged in one refresh (the IIT Bombay story went from four stories to one with 16 outlets).
2. **More, better sources.** Probed ~230 candidate feeds live; kept only those that parsed and were fresh. Indian national/city press,
   business dailies, fact-checkers, explainer outlets, Asia/Middle-East/Africa regional press, science/health/tech specialists.
   The front page now holds 200 instant stories (was 60) and **Everything** lists them all with topic, outlet and text filters.
3. **Lens — live voice.** A *talker–reasoner* design (the pattern used by production voice agents):
   * *Talker* — answers at once. Highlights, "why", "how did we get here", "what next" and "what happened with X" are read straight from stored explanations (≈1 s, no model call). Other questions retrieve stories by embedding, then stream one grounded answer.
   * *Reasoner* — while the talker speaks, searches the web, reads the best sources and adds "I dug a little deeper…" only if it found something new that the sources support.
   * Interruptible (Esc, or talk over it in headphone mode), keeps track of "the story we're on", works by typing too.
4. **UI**: command palette (Ctrl/⌘+K: jump anywhere, search all stories, or hand a question to Lens), Sources showcase, Everything, Talk to Lens, cards that glow under the cursor, Calm mode, WhatsApp share.

## 3. Honest limits

* Speech *input* uses the browser's `SpeechRecognition` (Chrome/Edge). It is not available in every browser and sends audio to the browser vendor's service, so it needs internet. Typing always works. A local Whisper model would remove that dependency.
* Latency: a question that needs the model waits for any model call already in flight on the shared GPU (up to ~30 s while the feed is being built). Highlights and explained stories are instant.
* Calm mode is a keyword classifier, not a judgement. It errs toward hiding.
* PIB/RBI/SEBI feeds exist but carry no dates, so they are not ingested (old releases would look new).
* Trust tiers are curated by hand and are a starting point, not a verdict.

## Sources

* Reuters Institute, *Digital News Report 2026* — [executive summary](https://reutersinstitute.politics.ox.ac.uk/digital-news-report/2026/dnr-executive-summary); [emerging uses of AI chatbots for news](https://reutersinstitute.politics.ox.ac.uk/digital-news-report/2026/emerging-uses-ai-chatbots-news-and-what-it-means-journalism)
* Reuters Institute, [Understanding young news audiences at a time of rapid change](https://reutersinstitute.politics.ox.ac.uk/understanding-young-news-audiences-time-rapid-change)
* India: [The Wire](https://m.thewire.in/article/media/digital-news-report-by-the-reuters-institute-avoid-news-digital-youtube-social-media-report), [Newslaundry](https://www.newslaundry.com/2026/06/18/58-news-from-youtube-56-via-whatsapp-reuters-report-on-indias-news-consumption-habits)
* AP-NORC / American Press Institute, [Fatigue, traditionalism, and engagement: Gen Z and Millennial news habits](https://apnorc.org/projects/fatigue-traditionalism-and-engagement-news-habits-and-attitudes-of-the-gen-z-and-millennial-generations/)
* Voice architecture: [LiveKit — the talker-reasoner pattern](https://livekit.com/blog/talker-reasoner-pattern-voice-agents); [VoiceAgentRAG (dual-agent)](https://arxiv.org/html/2603.02206v1)
