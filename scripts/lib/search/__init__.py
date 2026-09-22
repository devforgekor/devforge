# Status: production
# Path: imported by scripts/ modules
"""Search — web search (Brave) + local FTS5 + hybrid (BM25 + pgvector)."""
from lib.search.local_index import FTS5Index, get_index
from lib.search.hybrid import hybrid_search, bm25_only
