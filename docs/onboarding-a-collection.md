# Onboarding a new MongoDB collection

The query-generation and schema-context code (`app/db/introspect.py`,
`app/rag/schema_context.py`) already work generically per-collection — no code changes are
needed to add a new collection, just data and configuration.

## Steps

1. **Grant read access.** The Mongo user in `MONGODB_URI` must be able to read the new
   collection. It should be **read-only** at the database level, not just by convention — the
   app's own safety checks (`app/rag/validator.py`) are a second layer, not a substitute for a
   read-only credential.

2. **Add it to the allow-list.** Add the collection name to `MONGODB_ALLOWED_COLLECTIONS` in
   `.env` (comma-separated). Nothing outside this list is queryable, regardless of what Gemini
   generates — enforced in `validate_query_spec`.

3. **Re-run introspection** to sample the new collection's shape:

   ```bash
   python -m app.db.introspect --collections your_new_collection
   ```

   This merges into the existing `schema_summary.json` rather than overwriting it — other
   already-onboarded collections stay in the file. Omit `--collections` to re-profile every
   allowed collection at once instead.

4. **Annotate the schema.** Add an entry to `schema_annotations.json` (create it if it doesn't
   exist yet) describing what each field actually means — units, codes, relationships that
   aren't obvious from the field name and sampled examples alone. Follow the existing `orders`
   entry as a template. This is the single highest-leverage step for getting accurate answers:
   the LLM only knows what's in `schema_summary.json` + `schema_annotations.json`, nothing else
   about your data.

5. **No restart needed for schema changes.** `app/rag/pipeline.py` calls `build_schema_context()`
   fresh on every question, so updates to `schema_summary.json`/`schema_annotations.json` take
   effect immediately. Only `.env` changes (like adding to `MONGODB_ALLOWED_COLLECTIONS`) require
   restarting `python -m app.main`.

6. **Test before trusting it.** Ask a handful of real questions covering the new collection —
   a simple lookup, a calculation, and something ambiguous — and check the generated query
   (visible in the audit log, see `app/audit/logger.py`) actually makes sense before relying on
   it in a real channel.

## Verifying the introspection worked

```bash
python -m app.db.introspect --collections your_new_collection
cat schema_summary.json   # confirm the new collection's fields/types/examples look right
```

If `sampled_documents` is `0`, either the collection is empty or the Mongo user can't read it —
check both before assuming introspection is broken.
