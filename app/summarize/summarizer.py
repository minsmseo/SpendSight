"""Policy summary without AI: local HF model (optional) -> TextRank (sumy) -> lead sentences.

The LLM summary is produced together with the policy table in rules/policy_parser.py; this
module is the no-key chain behind it.
"""
import logging
import re

from app import config

log = logging.getLogger("spendsight.summarize")
LOCAL_MODEL = "Falconsai/text_summarization"      # t5-small based; light enough for a small instance
_pipeline = None


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"^\s*#+ .*$", "", text, flags=re.M)         # markdown headings are not sentences
    text = re.sub(r"[ \t]+", " ", text)
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}|\n(?=[-*•]\s)", text)
    return [re.sub(r"\s+", " ", p).strip(" -*•") for p in parts if len(p.strip()) > 25]


class _Tokenizer:
    """Regex tokenizer for sumy, so no NLTK data download is needed at runtime."""

    def to_sentences(self, text):
        return tuple(split_sentences(text))

    def to_words(self, sentence):
        return tuple(re.findall(r"[A-Za-z][A-Za-z'-]+", sentence))


def _textrank(text: str, n: int) -> str:
    from sumy.parsers.plaintext import PlaintextParser
    from sumy.summarizers.text_rank import TextRankSummarizer
    from sumy.utils import get_stop_words

    # Clean first (headings, bullets), because sumy joins a paragraph's lines before tokenizing.
    parser = PlaintextParser.from_string("\n".join(split_sentences(text)), _Tokenizer())
    summarizer = TextRankSummarizer()
    summarizer.stop_words = get_stop_words("english")
    return " ".join(str(s) for s in summarizer(parser.document, n))


def _local_model(text: str) -> str:
    """Hugging Face summarization pipeline: ~400-word chunks, then a summary of the summaries."""
    global _pipeline
    if _pipeline is None:
        from transformers import pipeline

        _pipeline = pipeline("summarization", model=LOCAL_MODEL)
    words = text.split()
    chunks = [" ".join(words[i:i + 400]) for i in range(0, len(words), 400)]
    parts = [_pipeline(c, max_length=90, min_length=20, truncation=True)[0]["summary_text"] for c in chunks]
    joined = " ".join(parts)
    if len(chunks) > 1 and len(joined.split()) > 180:
        joined = _pipeline(joined, max_length=180, min_length=60, truncation=True)[0]["summary_text"]
    return joined


def summarize(text: str, max_sentences: int = 7) -> dict:
    """Returns {"summary": str, "engine": "local_model" | "textrank" | "lead"}. Never raises."""
    if config.ENABLE_LOCAL_SUMMARIZER:
        try:
            summary = _local_model(text)
            if summary.strip():
                return {"summary": summary.strip(), "engine": "local_model"}
        except Exception as exc:
            log.warning("local summarizer unavailable (%s); using TextRank", type(exc).__name__)
    try:
        summary = _textrank(text, max_sentences)
        if summary.strip():
            return {"summary": summary, "engine": "textrank"}
    except Exception as exc:
        log.warning("TextRank failed (%s); using lead sentences", type(exc).__name__)
    return {"summary": " ".join(split_sentences(text)[:max_sentences]), "engine": "lead"}
