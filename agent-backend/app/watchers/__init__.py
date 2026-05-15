"""Long-lived background watchers that observe out-of-band signals (Kubernetes
event stream, etc.) and dispatch them into the existing analysis pipeline.

Watchers MUST NOT contain any analysis logic — they only construct an
``AnalysisRequest`` and hand it to :func:`app.core.agent.run_analysis`.
"""
