# Onboarding a new MongoDB collection

The schema-introspection and schema-context code (`app/db/introspect.py`,
`app/rag/schema_context.py`) already work generically per-collection. But the live agent pipeline
(`app/agents/graph.py`) only ever queries a collection through a registered **domain**
(`app/agents/domains.py::DOMAINS`) — the classifier only ever names a domain, never a raw
collection, so a collection with no domain registered for it is simply never reachable by a
question, regardless of `MONGODB_ALLOWED_COLLECTIONS`. Registering a domain **is** a (small) code
change; everything else below is data/configuration.

## Steps

1. **Grant read access.** The Mongo user in `MONGODB_URI` must be able to read the new
   collection. It should be **read-only** at the database level, not just by convention — the
   app's own safety checks (`app/rag/validator.py`) are a second layer, not a substitute for a
   read-only credential.

2. **Add it to the allow-list.** Add the collection name to `MONGODB_ALLOWED_COLLECTIONS` in
   `.env` (comma-separated). Nothing outside this list is queryable, regardless of what Gemini
   generates — enforced in `validate_query_spec`. Note this is a *ceiling*, not what actually
   makes a collection reachable — see the next step.

3. **Register a domain for it** in `app/agents/domains.py::DOMAINS` — a `DomainConfig` with at
   least `name` and `collection`; set `usertype` only if this collection is shared with another
   domain the way `users` is shared between `customers`/`vendors`, `geo_capable`/`geo_field` if
   it supports "nearby" questions, and `schema_fields` if the agent should only see a subset of
   the collection's fields (omit it to show the whole collection). Also add the new domain name
   to `app/agents/classifier.py`'s `DomainName` literal type and its prompt's domain list, so the
   classifier can actually name it. This is the step that makes the new collection reachable by a
   question at all.

4. **Re-run introspection** to sample the new collection's shape:

   ```bash
   python -m app.db.introspect --collections your_new_collection
   ```

   This merges into the existing `schema_summary.json` rather than overwriting it — other
   already-onboarded collections stay in the file. Omit `--collections` to re-profile every
   allowed collection at once instead.

5. **Annotate the schema.** Add an entry to `schema_annotations.json` (create it if it doesn't
   exist yet) describing what each field actually means — units, codes, relationships that
   aren't obvious from the field name and sampled examples alone. Follow the existing `orders`
   entry as a template. This is the single highest-leverage step for getting accurate answers:
   the LLM only knows what's in `schema_summary.json` + `schema_annotations.json`, nothing else
   about your data.

6. **Restart needed for the domain registration; not for schema/annotation edits.**
   `app/agents/query_agents.py` re-reads `schema_summary.json`/`schema_annotations.json` on every
   question (mtime-cached — see `app/rag/schema_context.py`), so edits to those two files take
   effect immediately, no restart. Step 3 (registering the domain) and step 2 (`.env` changes
   like `MONGODB_ALLOWED_COLLECTIONS`) are both regular process state, though, so they *do*
   require restarting `python -m app.main`.

7. **Test before trusting it.** Ask a handful of real questions covering the new collection —
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
