"""Isolated NER worker: loads GLiNER once and answers JSON-lines requests on stdin/stdout.

Runs as a separate process (`python -m backend.services.ner_worker`) so that a native crash or out-of-memory kill in
torch/GLiNER can never take the API server down. Protocol (one JSON object per line):
  ← {"ready": true}                                     (once the model is loaded)
  → {"id": 1, "texts": [...], "labels": [...], "threshold": 0.55}
  ← {"id": 1, "preds": [[{"text","label","score"}, ...], ...]}
"""

import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

_proto = sys.stdout            # protocol channel
sys.stdout = sys.stderr        # anything libraries print goes to stderr


def _send(obj: dict) -> None:
    _proto.write(json.dumps(obj) + "\n")
    _proto.flush()


def main() -> None:
    from gliner import GLiNER
    model = GLiNER.from_pretrained("urchade/gliner_small-v2.1")
    _send({"ready": True})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        out = []
        for text in req["texts"]:
            try:
                preds = model.predict_entities(text, req["labels"], threshold=req.get("threshold", 0.55))
                out.append([{"text": p["text"], "label": p["label"], "score": float(p["score"])} for p in preds])
            except Exception:  # noqa: BLE001 — one bad text must not kill the worker
                out.append([])
        _send({"id": req["id"], "preds": out})


if __name__ == "__main__":
    main()
