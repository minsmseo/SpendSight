"""Policy summary: LLM -> local HF model (ENABLE_LOCAL_SUMMARIZER=1) -> TextRank (sumy). TODO(step 6)."""
import re


def summarize(text: str, max_sentences: int = 7) -> dict:
    """Returns {"summary": str, "engine": "llm" | "local_model" | "textrank" | "lead"}."""
    # TODO(step 6): LLM, then optional transformers pipeline, then sumy TextRank.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]
    return {"summary": " ".join(sentences[:max_sentences]), "engine": "lead"}
