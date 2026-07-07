# knowledge_base/

This folder holds plain-text files that Open Engine's memory layer indexes
into `db.sqlite` via SQLite FTS5 keyword search.

## Format

- **File types**: `.md` (Markdown) or `.txt` (plain text)
- **One topic per file**: each file becomes one row in `knowledge_documents` with `kind='rule'`
- **Title**: the first line starting with `# ` (H1 heading) is used as the document title.
  If no H1 is present, the filename (without extension) becomes the title.
- **Content**: the full file text is indexed; the entire content is stored and searchable.

## Example

```
knowledge_base/
├── coding-standards.md      # first line: "# Python Coding Standards"
├── project-conventions.txt  # no H1, title = "project-conventions"
└── api-guidelines.md
```

## Indexing

Files are indexed (or re-indexed) by clicking the **Reindex knowledge_base** button in the
Open Engine dashboard, or by calling `POST /api/knowledge/reindex`.

Reindexing is **idempotent**: if a file's content hasn't changed since the last index run,
its row is not modified (the `updated_at` timestamp stays unchanged). Only changed or new
files create/update database rows.

## Search

The knowledge base is searched automatically at the start of every task run (RESEARCH stage),
and the retrieved snippets are passed to Claude for SPEC and PLANNING stages.

You can also search manually via `GET /api/knowledge/search?q=your+query`.

## Vector search (optional)

By default, search uses SQLite FTS5 keyword matching. To enable semantic (vector) search,
set `embedding_provider` in `config/memory.json` to a provider config object:

```json
{
  "embedding_provider": {
    "kind": "gemini",
    "model": "text-embedding-004",
    "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent",
    "api_key_env": "GEMINI_API_KEY"
  },
  "top_k": 5
}
```

The API key is always read from the named environment variable — never hardcode it.
If the vector tier is unavailable or the provider call fails, search falls back to FTS5 silently.
