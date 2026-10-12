# Competitor study: what the news apps do, what users complain about, what ContextLens took

Researched 2026-10-06 from public pages (links below). App-store reviews were read through review-summary sites, not scraped, so treat the complaint counts as themes, not statistics.

## What each app is best at

| App | Architecture / idea | Best at | Users complain about |
|---|---|---|---|
| **Inshorts** | AI-first ingestion over thousands of sources, NLP tagging, humans edit; "Rapid60" model trained on 500k+ editor-written 60-word summaries | Speed: a story in 60 words | Ad density (#1 theme), "not interested" topics still shown, clickbaity wording, perceived bias |
| **Google News** | Story = cluster of articles; Full Coverage is un-personalised (language/region only); For You uses click-based collaborative filtering (PLSI, MinHash, MapReduce); separate Headlines, Following, Local sections | Breadth, clustering, a Local section | Opaque algorithm, ads, paywalled links, clickbait |
| **Ground News** | Clusters 50k+ sources into one story; bias per outlet = average of AllSides, Ad Fontes, Media Bias Fact Check; **Blindspot** = stories one side barely covers; no personalisation algorithm | Seeing who covered what | US-centric, notifications stop, subscription friction, links with ads/paywalls |
| **Particle** | Multi-source AI summaries with citations, "Opposite Sides", Q&A, audio, summary styles | Depth and polish | Search unreliable, cannot turn off US news, feature overload, engagement-leaning personalisation |
| **SmartNews / NewsBreak** | ML ranking pipelines, local news (NewsBreak down to ZIP code) | Local relevance | Phone heating, notifications that do not match the top stories |
| **Artifact** (closed 2024) | AI-personalised reader | Loved by a core group | No big enough market, could not grow outside the US |

## Already in ContextLens (so nothing to copy)
- Inshorts-style quick read: **Flow** (swipe deck: gist, why, history, what next); timed briefing.
- Google Full Coverage and Ground-style coverage spread: the story page's **Coverage** with per-outlet framing, details only one outlet reported, and countries that covered it.
- No ads, a fail-closed editor gate against clickbait, "why you're seeing this", Calm mode, follow-a-story, Q&A and voice.

## Taken from the study (shipped)
1. **Local news shelf** (Google News Local, NewsBreak, SmartNews): Today shows "Near you" for a home place the reader picks. It combines stories and headlines placed there with the Globe's stored live local headlines. `GET/PUT/DELETE /api/local`, `GET /api/local/search`.
2. **Blind spots** (Ground News): stories covered by 3+ outlets that all sit in ONE country's press. `GET /api/blindspots`. It is geographic on purpose: no political-bias ratings are used.

3. **In short** (Inshorts): `/shorts`, one story per screen in under 60 words, cut at a sentence end from the story's own validated text; keyboard and swipe, share button. `GET /api/shorts`.
4. **Breaking alerts** (Google/Apple News, Inshorts): desktop notifications for stories whose Signal clears the reader's bar, capped per hour, quiet overnight, never repeated. Settings > Feed. `GET /api/alerts`.

5. **Vernacular languages** (Dailyhunt, Inshorts Hindi): language picker on In short (Hindi, Marathi, Bengali, Tamil, Telugu, Gujarati, Kannada, Malayalam). `POST /api/translate/{id}` translates headline + summary on demand and caches it in `stories.extras.tr`. A fail-closed gate refuses output that is not in the target script, has a wrong length, or changes any number; the card then stays English with a note.
6. **One-tap spoken briefing**: "Play briefing" on In short reads the top 8 cards aloud with the browser's on-device voices, in the chosen language where a matching voice exists.

## Still missing (and why)
- Nothing from the study is left except what was deliberately not copied (below). Known limits: the translation gate checks script, length and numbers but not names, and a spoken non-English briefing needs an installed voice for that language in the browser/OS.

## Deliberately not copied
- **Political bias ratings**: they need licensed third-party ratings (AllSides/Ad Fontes/MBFC) and would label outlets we cannot verify.
- **Server push notifications**: this is a local, single-user app with no push service. The alerts shipped are in-browser and only while the app is open, so they cannot drift from what the app shows.
- **Click-based collaborative filtering**: needs many users. ContextLens personalises from the reader's own interests and feedback.

## Sources
- Inshorts: [Rapid60 announcement](https://www.prnewswire.com/in/news-releases/inshorts-introduces-ai-based-news-summarization-on-its-app-aims-to-generate-over-100k-shorts-per-month-685223571.html), [Inshorts reviews summary](https://marlvel.ai/apps/inshorts)
- Google News: [Scalable online collaborative filtering paper](https://www.ra.ethz.ch/cdstore/www2007/www2007.org/papers/paper570.pdf), [How stories are selected](https://support.google.com/googlenews/answer/9005749?hl=en)
- Ground News: [Rating system](https://ground.news/rating-system), [FAQ](https://ground.news/frequently-asked-questions)
- Particle: [review](https://wisp.news/blog/particle-review/); Artifact: [why it failed](https://newsletter.failory.com/p/why-artifact-failed)
- SmartNews / NewsBreak: [comparison](https://themagpieproject.com/news-aggregators)
