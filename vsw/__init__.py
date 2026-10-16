"""Visual Search Workbench — 横向视觉搜索工作台.

A local workbench that turns scattered reverse-image-search clicks into a managed,
comparable, iterable batch workflow. The engine discovers; the workbench schedules,
deduplicates, ranks and accumulates.

Modules
-------
``imaging``  intake, SHA-256/pHash/dHash/aHash, query-variant generation
``engine``   local SigLIP2 vectors + BLIP captions (semantic layer)
``store``    SQLite schema: seeds/variants/jobs/sources/findings/clusters/ledger
``engines``  external engine dispatch links + result-page parsing + source aggregation
``service``  the closed loop: intake -> queue -> dedup/cluster -> rank -> promote
``server``   stdlib HTTP server: JSON API + single-page WebUI
"""
from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["imaging", "engine", "store", "engines", "service", "server"]
