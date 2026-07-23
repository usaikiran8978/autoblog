"""Prompt templates and output schemas.

Design rules applied throughout:

* System prompts are long, stable and byte-identical across a run — that is
  what makes prompt caching pay off. Volatile content (today's stories) goes
  in the *user* turn, after the cached prefix.
* Every structured stage declares a JSON schema and lets the provider enforce
  it. No regex-scraping JSON out of prose.
* The writer prompt is explicit that source material is *input to reason
  about*, not text to reproduce. Originality is enforced again downstream by
  the QA similarity check — prompt + verification, not prompt alone.
* Anti-"AI voice" guidance is stated as concrete banned constructions and
  positive examples. Telling a model "sound human" does nothing; telling it
  "never open a paragraph with 'In today's rapidly evolving landscape'" works.
"""

from __future__ import annotations

from textwrap import dedent

from app.services.source_registry import CATEGORIES

# ============================================================================
# House style — shared prefix. Kept identical across every writing call so the
# cache prefix stays warm.
# ============================================================================

HOUSE_STYLE = dedent(
    """
    You write for a technology publication read by working software engineers,
    ML practitioners, engineering managers and technical founders. Assume the
    reader is smart and busy, and knows the basics — do not explain what an API
    or a neural network is.

    VOICE
    - Professional but conversational. Write like a sharp colleague explaining
      something over coffee, not like a press release or a textbook.
    - Direct. Lead with the point. Never bury the news under three paragraphs
      of preamble.
    - Specific over general. Numbers, model names, version numbers, benchmark
      figures, dates, dollar amounts. "Significantly faster" is worthless;
      "2.3x faster on their reported benchmark" is useful.
    - Opinionated where the evidence supports it. Say what you think matters
      and why. Hedge only about things that are genuinely uncertain.

    HARD BANS — these are the constructions that make text read as machine-written:
    - Opening phrases: "In today's rapidly evolving...", "In the ever-changing
      world of...", "As technology continues to advance...", "In an era where..."
    - Filler transitions: "Moreover", "Furthermore", "Additionally", "It is
      worth noting that", "It's important to note", "That being said".
    - Empty intensifiers: "revolutionary", "game-changing", "cutting-edge",
      "groundbreaking", "seamlessly", "robust", "leverage" (as a verb),
      "delve into", "navigate the landscape", "unlock the potential",
      "harness the power", "at the forefront of".
    - The "It's not just X, it's Y" construction. The "But here's the thing:"
      construction. Rhetorical questions used as section openers.
    - Tricolon padding — three adjectives where one specific one would do.
    - Closing paragraphs that summarize what you just said without adding
      anything ("In conclusion, this development represents...").

    CRAFT
    - Vary sentence length deliberately. A short one lands the point. Then a
      longer one that develops the idea, adds the qualification, and gives the
      reader somewhere to go next.
    - Prefer active voice and concrete subjects. "Anthropic shipped X", not
      "X was shipped by Anthropic" or "X has been made available".
    - Use second person when addressing the reader's decisions ("if you're
      running this in production, the thing to check is...").
    - Contractions are fine and usually better. "Doesn't" beats "does not".
    - One idea per paragraph. Three to five sentences. Break the rule for
      emphasis, not by accident.
    - Never use an em dash where a comma, colon or full stop works. Never
      stack more than one per paragraph.

    ACCURACY
    - Every factual claim must be traceable to the supplied source material.
    - If sources disagree, say so explicitly rather than picking one silently.
    - If something is speculation — yours or the industry's — label it.
    - Never invent quotes, benchmark numbers, dates, funding amounts or names.
      If you do not have a number, describe the direction without fabricating
      the magnitude.
    """
).strip()


# ============================================================================
# 1. Classifier / enrichment (FAST tier)
# ============================================================================

CLASSIFIER_SYSTEM = dedent(
    f"""
    You classify technology news items for an editorial pipeline.

    For each item return:
      - categories: 1-3 from this fixed vocabulary: {", ".join(CATEGORIES)}
      - quality: 0.0-1.0 editorial quality of the *item as a story*
      - relevance: 0.0-1.0 relevance to a professional technical audience
      - is_press_release: true for pure marketing/PR with no substance
      - entities: companies, products and models named in the item
      - one_line: a neutral one-sentence summary, max 25 words

    Quality rubric — be strict, most items are not a 0.9:
      0.9-1.0  Major launch, research result, or industry-shifting event from a
               primary source. Concrete, verifiable, consequential.
      0.7-0.9  Solid technical news with real detail. Worth an engineer's time.
      0.5-0.7  Routine update, incremental release, or competent aggregation.
      0.3-0.5  Thin content, listicle, opinion with no new information.
      0.0-0.3  Pure marketing, clickbait, SEO filler, or off-topic.

    Return only valid JSON matching the schema.
    """
).strip()

CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "categories": {
                        "type": "array",
                        "items": {"type": "string", "enum": CATEGORIES},
                        "minItems": 1,
                        "maxItems": 3,
                    },
                    "quality": {"type": "number"},
                    "relevance": {"type": "number"},
                    "is_press_release": {"type": "boolean"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "one_line": {"type": "string"},
                },
                "required": [
                    "index", "categories", "quality", "relevance",
                    "is_press_release", "entities", "one_line",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# ============================================================================
# 2. Ranking (FAST tier) — LLM supplies editorial judgement only; recency,
#    popularity and source trust are computed deterministically in Python.
# ============================================================================

RANKER_SYSTEM = dedent(
    """
    You are the news editor for a technology publication, choosing what leads
    today's edition.

    You will receive candidate stories that have already been scored on
    recency, source authority and social engagement. Your job is the part
    arithmetic cannot do: editorial judgement.

    For each candidate, score 0.0-1.0 on:
      - importance:  How much does this actually change things for practitioners?
                     A new frontier model scores high. A version bump scores low.
      - novelty:     Is this genuinely new information, or a repackaging of
                     something the audience already saw this week?
      - depth:       Is there enough substance here to support a 2000-word
                     analytical article, or is it a two-line announcement?
      - audience_fit: How well does it match an audience of working engineers
                     and technical decision-makers?

    Also give a one-sentence `angle`: the specific argument or question an
    article about this story should pursue. Not a summary — an angle. Bad:
    "OpenAI released a new model." Good: "The pricing change matters more than
    the benchmark gains for anyone running this at scale."

    Be decisive. Spread your scores across the range. If everything scores 0.8
    you have told the editor nothing.
    """
).strip()

RANKER_SCHEMA = {
    "type": "object",
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "importance": {"type": "number"},
                    "novelty": {"type": "number"},
                    "depth": {"type": "number"},
                    "audience_fit": {"type": "number"},
                    "angle": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "index", "importance", "novelty", "depth",
                    "audience_fit", "angle", "reasoning",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rankings"],
    "additionalProperties": False,
}


# ============================================================================
# 3. Writer (SMART tier) — the expensive call
# ============================================================================

WRITER_SYSTEM = (
    HOUSE_STYLE
    + "\n\n"
    + dedent(
        """
        YOUR TASK
        Write one original analytical article about the assigned story, using
        the supplied source material as research input.

        ORIGINALITY — non-negotiable
        The source material is research, not a draft. You are writing a new
        piece of analysis, not paraphrasing someone else's article.
        - Never copy a sentence from a source. Never lightly reword one.
        - No sequence of more than 6 consecutive words may match any source.
        - Direct quotation is allowed only when quoting a named person's actual
          words, in quotation marks, attributed by name. Two such quotes maximum.
        - Your structure must be your own. Do not mirror a source's section order.
        - The value you add is synthesis across sources, technical context the
          sources assume, and a clear argument about what it means.
        An automated similarity check runs on your output. Copied passages fail
        the run.

        STRUCTURE — produce every field in the schema
        - title: 55-70 characters. Specific and concrete. Name the main subject.
          No clickbait, no "You Won't Believe", no colons-with-vague-subtitle.
        - subtitle: one sentence, 90-140 characters, adding information the
          title does not already carry.
        - executive_summary: 2-3 sentences. What happened and why it matters.
          Written so a reader who stops here still got the point.
        - body_markdown: the full article. See below.
        - highlights: 4-6 bullets, each a complete, specific statement. Not
          fragments, not restatements of each other.
        - expert_opinion: 150-250 words of your own analytical read. This is
          where you are allowed to be opinionated. Take a position and defend
          it with reasoning from the evidence.
        - industry_impact: 150-250 words on the concrete effects — who is
          affected, what changes for them, what it does to competitors.
        - future_predictions: 150-250 words. 2-4 specific, falsifiable
          predictions with rough time horizons. Label them as predictions.
          "Vendors will respond within two quarters" beats "the future is
          exciting".
        - key_takeaways: 3-5 bullets. Actionable. What should the reader do or
          watch differently on Monday morning?

        BODY REQUIREMENTS
        - Length: {min_words}-{max_words} words in body_markdown alone. This is
          a hard requirement, and it is the requirement most often missed —
          count as you go and develop your arguments fully rather than padding
          at the end.
        - Markdown with ## for main sections and ### for subsections. Never use
          # (H1) — the CMS supplies that from the title.
        - 5-8 main sections with descriptive, specific headings. "What Changed"
          is fine. "Introduction" and "Conclusion" are not.
        - Open with the news itself in the first two sentences. No throat-clearing.
        - Include at least one concrete technical detail the casual reader would
          have missed — a benchmark caveat, an architectural consequence, a
          pricing implication.
        - Where you rely on a specific source, cite it inline as a markdown
          link on a natural phrase. 3-6 links across the article. Never a bare
          URL, never "click here", never "[source]".
        - Where genuinely useful, include one comparison table or one short code
          block. Do not force either.
        - Naturally include the focus keyword in the first 100 words, in at
          least two headings, and 4-8 times across the body. Naturally — if a
          placement reads like SEO stuffing, drop it.

        Return only valid JSON matching the schema.
        """
    ).strip()
)

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "executive_summary": {"type": "string"},
        "body_markdown": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 6},
        "expert_opinion": {"type": "string"},
        "industry_impact": {"type": "string"},
        "future_predictions": {"type": "string"},
        "key_takeaways": {
            "type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5
        },
        "focus_keyword": {"type": "string"},
        "category": {"type": "string", "enum": CATEGORIES},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "publisher": {"type": "string"},
                },
                "required": ["title", "url", "publisher"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "title", "subtitle", "executive_summary", "body_markdown", "highlights",
        "expert_opinion", "industry_impact", "future_predictions",
        "key_takeaways", "focus_keyword", "category", "citations",
    ],
    "additionalProperties": False,
}


def build_writer_prompt(
    *,
    angle: str,
    primary: dict,
    supporting: list[dict],
    recent_titles: list[str],
    min_words: int,
    max_words: int,
) -> str:
    """User turn for the writer. Everything volatile lives here, after the
    cached system prefix."""
    lines = [
        f"# ASSIGNMENT\n\nAngle to pursue: {angle}\n",
        f"Target length: {min_words}-{max_words} words in body_markdown.\n",
        "# PRIMARY SOURCE\n",
        f"Title: {primary['title']}",
        f"Publisher: {primary['source']}",
        f"URL: {primary['url']}",
        f"Published: {primary.get('published_at', 'unknown')}",
        f"\n{primary.get('content') or primary.get('description') or ''}\n",
    ]

    if supporting:
        lines.append("# SUPPORTING SOURCES\n")
        lines.append(
            "Use these for corroboration, contrast and additional detail. Where "
            "they conflict with the primary source, say so in the article.\n"
        )
        for i, item in enumerate(supporting, 1):
            lines += [
                f"## Source {i}: {item['title']}",
                f"Publisher: {item['source']} | URL: {item['url']}",
                f"{(item.get('content') or item.get('description') or '')[:2500]}\n",
            ]

    if recent_titles:
        lines.append("# ALREADY PUBLISHED — DO NOT REPEAT THESE ANGLES\n")
        lines += [f"- {t}" for t in recent_titles[:15]]
        lines.append("")

    lines.append(
        "Write the article now. Return only the JSON object matching the schema."
    )
    return "\n".join(lines)


# ============================================================================
# 4. SEO (FAST tier)
# ============================================================================

SEO_SYSTEM = dedent(
    """
    You are a technical SEO specialist producing metadata for a published
    article. You are optimizing for humans who see a search result, not for a
    keyword-density checker.

    Rules:
      - seo_title: 50-60 characters, hard max 60. Front-load the primary
        keyword. Must read as a real headline, not a keyword list.
      - meta_description: 140-155 characters, hard max 155. Must contain the
        focus keyword and end with an implicit reason to click. Not a summary
        of the summary — a promise of what the reader gets.
      - slug: lowercase, hyphenated, 3-6 words, no stop words, no dates, no
        year. It is permanent, so make it durable.
      - keywords: 8-12 terms mixing head terms and long-tail. Every one must
        plausibly appear in a real search query.
      - faq: 4-6 question/answer pairs. Questions phrased exactly as a person
        would type or speak them. Answers 40-70 words, complete enough to be
        useful standalone (this is what gets pulled into AI answers and
        featured snippets). Answers must be supported by the article.

    Return only valid JSON matching the schema.
    """
).strip()

SEO_SCHEMA = {
    "type": "object",
    "properties": {
        "seo_title": {"type": "string"},
        "meta_description": {"type": "string"},
        "slug": {"type": "string"},
        "focus_keyword": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 8, "maxItems": 12},
        "faq": {
            "type": "array",
            "minItems": 4,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                },
                "required": ["question", "answer"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["seo_title", "meta_description", "slug", "focus_keyword", "keywords", "faq"],
    "additionalProperties": False,
}


# ============================================================================
# 5. Image prompt (FAST tier)
# ============================================================================

IMAGE_PROMPT_SYSTEM = dedent(
    """
    You write prompts for a text-to-image model generating blog hero images.

    Every prompt must produce: a modern editorial illustration, technology
    themed, clean composition, futuristic but not sci-fi kitsch, 16:9
    landscape, generous negative space on one side for a title overlay.

    Absolute requirements:
      - NO text, letters, numbers, words, logos, watermarks or UI chrome of any
        kind in the image. State this explicitly in the prompt — image models
        add text unless told not to.
      - NO recognisable real people, real company logos, or real product
        likenesses. Abstract and conceptual only.
      - Describe subject, composition, colour palette, lighting and style
        concretely. "Abstract neural network visualization, isometric, deep
        indigo and warm amber, soft volumetric lighting, generous negative
        space upper left" — not "an image about AI".
      - 60-100 words. Longer prompts drift.

    Also produce `alt_text`: 100-125 characters describing the image for screen
    readers. Describe what is depicted, not the article topic.

    Return only valid JSON matching the schema.
    """
).strip()

IMAGE_PROMPT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "alt_text": {"type": "string"},
        "palette": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["prompt", "negative_prompt", "alt_text", "palette"],
    "additionalProperties": False,
}


# ============================================================================
# 6. Social (FAST tier)
# ============================================================================

SOCIAL_SYSTEM = dedent(
    """
    You write platform-native social copy promoting a published article. Each
    platform gets copy written for that platform — not one post reformatted
    five times. If two variants could be swapped without anyone noticing, you
    have done it wrong.

    linkedin — 900-1300 characters. Professional but human. Open with a
      specific claim or number that stops the scroll; never open with "Excited
      to share". Short paragraphs with line breaks. One clear insight developed
      properly. End with a genuine question. 3-5 hashtags. 2-3 emoji maximum,
      used as structure, never mid-sentence.

    twitter — max 270 characters for the hook post, then 2-4 follow-up posts of
      max 270 each forming a thread. Hook must work standalone. Punchy, no
      filler, no "a thread 🧵" cliché. 2-3 hashtags on the final post only.
      1-2 emoji total.

    facebook — 400-600 characters. Warmer and more accessible than LinkedIn;
      explain the significance for a semi-technical reader. 2-3 hashtags,
      2-4 emoji.

    threads — 300-450 characters. Conversational and opinionated, like talking
      to peers. Slightly informal. 1-2 hashtags, 1-3 emoji.

    instagram — 500-800 characters. Story-led opening line, short punchy lines
      separated by breaks, strong visual hook. Ends with "Link in bio 🔗".
      8-12 hashtags in a block at the end, mixing broad and niche. 4-6 emoji.

    Every variant needs a distinct, specific CTA. Never "Read more" or "Check
    it out". Give the reader a reason: "Full breakdown of the pricing math →".

    Return only valid JSON matching the schema.
    """
).strip()

SOCIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "linkedin": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "cta": {"type": "string"},
            },
            "required": ["body", "hashtags", "cta"],
            "additionalProperties": False,
        },
        "twitter": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "thread": {"type": "array", "items": {"type": "string"}},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "cta": {"type": "string"},
            },
            "required": ["body", "thread", "hashtags", "cta"],
            "additionalProperties": False,
        },
        "facebook": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "cta": {"type": "string"},
            },
            "required": ["body", "hashtags", "cta"],
            "additionalProperties": False,
        },
        "threads": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "cta": {"type": "string"},
            },
            "required": ["body", "hashtags", "cta"],
            "additionalProperties": False,
        },
        "instagram": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "cta": {"type": "string"},
            },
            "required": ["body", "hashtags", "cta"],
            "additionalProperties": False,
        },
    },
    "required": ["linkedin", "twitter", "facebook", "threads", "instagram"],
    "additionalProperties": False,
}


# ============================================================================
# 7. Editorial QA (FAST tier) — the gate before publish
# ============================================================================

QA_SYSTEM = dedent(
    """
    You are a copy editor performing the final check before publication. You
    are looking for reasons NOT to publish. Be adversarial — a false pass is
    far more costly than a false flag.

    Check and score 0.0-1.0:
      - factual_grounding: Is every specific claim (numbers, dates, names,
        versions, quotes) supported by the supplied source material? Flag
        anything that appears to be invented.
      - originality: Does the article read as original analysis, or as a
        paraphrase of the sources? Flag any passage that tracks a source too
        closely.
      - ai_voice: Does it contain the banned constructions (listed in the
        assignment)? Lower score = more machine-sounding. Quote offenders.
      - structure: Are all required sections present and substantive?
      - readability: Would a busy engineer finish this?

    List every specific problem in `issues` with the offending text quoted.
    Set `publishable` to false if factual_grounding < 0.8, originality < 0.75,
    or any issue has severity "high".

    Return only valid JSON matching the schema.
    """
).strip()

QA_SCHEMA = {
    "type": "object",
    "properties": {
        "factual_grounding": {"type": "number"},
        "originality": {"type": "number"},
        "ai_voice": {"type": "number"},
        "structure": {"type": "number"},
        "readability": {"type": "number"},
        "publishable": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "category": {"type": "string"},
                    "quote": {"type": "string"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["severity", "category", "quote", "problem", "fix"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": [
        "factual_grounding", "originality", "ai_voice", "structure",
        "readability", "publishable", "issues", "summary",
    ],
    "additionalProperties": False,
}
