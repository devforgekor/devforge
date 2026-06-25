#!/usr/bin/env python3
# Status: deprecated
# Path: replaced by LLM self-verify (enrich.py _verify_tldr, day_verify.py _llm_verify_entities, extract.py _llm_nli_check) — DeBERTa-v3 NLI port 8085 never deployed, remove after 2026-07
"""NLI Cross-Encoder Server — DeBERTa-v3-large (DEPRECATED, never deployed).

LLM self-verify in enrich.py / day_verify.py / extract.py replaces this.
systemd devforge-nli.service is disabled.""

import os
import sys
import json
import logging
from typing import Dict

import torch
import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field
from transformers import AutoTokenizer, AutoModelForSequenceClassification

# ── Config ──────────────────────────────────────────────────────────────────
MODEL_PATH = "/opt/ai_data/models/nli-deberta-v3-large"
PORT = int(os.environ.get("NLI_PORT", "8085"))
HOST = os.environ.get("NLI_HOST", "127.0.0.1")
MAX_LENGTH = 512
DEVICE = "cpu"

# Map cross-encoder logits → label
ID2LABEL = {0: "CONTRADICTION", 1: "ENTAILMENT", 2: "NEUTRAL"}
# Our binary grouping: ENTAILMENT → SUPPORTED, else → NOT_SUPPORTED
LABEL_MAP = {"ENTAILMENT": "SUPPORTED", "CONTRADICTION": "NOT_SUPPORTED", "NEUTRAL": "NOT_SUPPORTED"}

app = FastAPI(title="NLI Cross-Encoder", version="1.0.0")

# Global model reference — loaded at startup
_model = None
_tokenizer = None


class NLIRequest(BaseModel):
    source: str = Field(..., description="Source document text (context)")
    evidence: str = Field(..., description="Claim/evidence to verify against source")
    strict: bool = Field(False, description="If True, NEUTRAL → NOT_SUPPORTED. If False, NEUTRAL returned as-is.")


class NLIResponse(BaseModel):
    label: str = Field(..., description="SUPPORTED | NOT_SUPPORTED | NEUTRAL")
    label_3class: str = Field(..., description="ENTAILMENT | CONTRADICTION | NEUTRAL")
    score: float = Field(..., ge=0.0, le=1.0, description="Softmax probability of predicted class")
    scores: Dict[str, float] = Field(..., description="All class probabilities")


def softmax(logits: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.softmax(logits, dim=-1)


@app.on_event("startup")
def load_model():
    global _model, _tokenizer
    logging.info(f"Loading model from {MODEL_PATH}...")
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    _model = AutoModelForSequenceClassification.from_pretrained(MODEL_PATH)
    _model.eval()
    _model.to(DEVICE)
    n_params = sum(p.numel() for p in _model.parameters())
    logging.info(f"Model loaded: {n_params:,} params, device={DEVICE}")


@app.get("/health")
def health():
    return {"status": "ok", "model": "cross-encoder/nli-deberta-v3-large", "device": DEVICE}


@app.post("/nli", response_model=NLIResponse)
def nli(req: NLIRequest) -> Dict:
    """Run NLI cross-encoder on (source, evidence) pair.

    Returns 3-class label (ENTAILMENT/CONTRADICTION/NEUTRAL)
    and binary SUPPORTED/NOT_SUPPORTED mapping.
    """
    if _model is None or _tokenizer is None:
        return {"label": "NEUTRAL", "label_3class": "NEUTRAL",
                "score": 0.0, "scores": {"ENTAILMENT": 0.0, "CONTRADICTION": 0.0, "NEUTRAL": 1.0}}

    # Truncate to prevent OOM on very long sequences
    source_trunc = req.source[:4000] if len(req.source) > 4000 else req.source
    evidence_trunc = req.evidence[:1000] if len(req.evidence) > 1000 else req.evidence

    with torch.no_grad():
        inputs = _tokenizer(
            evidence_trunc, source_trunc,
            return_tensors="pt",
            truncation=True,
            max_length=MAX_LENGTH,
            padding=True,
        ).to(DEVICE)

        outputs = _model(**inputs)
        logits = outputs.logits
        probs = softmax(logits)[0]

    scores = {
        ID2LABEL[i]: round(float(probs[i]), 4)
        for i in range(3)
    }
    pred_label = ID2LABEL[int(logits.argmax(dim=-1)[0])]
    score = scores[pred_label]

    # Binary mapping
    binary = LABEL_MAP[pred_label]
    if req.strict and pred_label == "NEUTRAL":
        binary = "NOT_SUPPORTED"

    return {
        "label": binary,
        "label_3class": pred_label,
        "score": score,
        "scores": scores,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logging.info(f"Starting NLI server on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
