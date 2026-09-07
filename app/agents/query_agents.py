"""Per-domain LangChain chain: schema-scoped prompt -> validated-shape QuerySpec | QueryError.

Each domain agent only ever sees its own domain's schema (app/rag/schema_context.py ::
build_domain_schema_context, scoped via app/agents/domains.py) and its output is immediately
passed through scope_spec_to_domain() -- the collection and usertype filter it emits are
overwritten by code before validation, so nothing this chain generates can escape its domain
even if the prompt itself is defeated.
"""

from datetime import datetime, timezone

from langchain_core.prompts import PromptTemplate

from app.agents.domains import DomainConfig, scope_spec_to_domain
from app.llm.gemini_client import GeminiClient
from app.rag.query_spec import QueryError, QuerySpec
from app.rag.schema_context import build_domain_schema_context

_PROMPT = PromptTemplate.from_template(
    "Today's date (UTC): {today}\n\n"
    "You translate a question into a single structured MongoDB query against the '{collection}' "
    "collection, scoped to the '{domain}' domain.\n\n"
    "Schema (only the fields relevant to this domain):\n{schema_context}\n\n"
    "Rules:\n"
    "- Respond with ONLY a JSON object, no prose, no markdown fences.\n"
    "- If the question cannot be answered from this schema, respond with: "
    '{{"error": "<short reason>"}}\n'
    "- Otherwise respond with an object matching this shape:\n"
    "  {{\n"
    '    "collection": "{collection}",\n'
    '    "operation": "find" | "aggregate" | "count",\n'
    '    "filter": {{}},\n'
    '    "pipeline": [],\n'
    '    "projection": null,\n'
    '    "sort": null,\n'
    '    "limit": 50,\n'
    '    "start_date": null,\n'
    '    "end_date": null{geo_field_doc}\n'
    "  }}\n"
    "- If the question implies a date range, restrict it in filter/pipeline AND set "
    "start_date/end_date to describe that same range.\n"
    '- Date values in filter/pipeline MUST be ISO-8601 strings (e.g. "2026-09-01T00:00:00Z") '
    "-- MongoDB will compare them correctly against stored datetimes.\n"
    "- Never use $where, $function, $accumulator, $merge, or $out.\n"
    "- Never filter or set anything on a 'usertype' field yourself -- that's applied "
    "automatically for this domain.\n"
    "- If your pipeline uses $group, always follow it with a $project that renames the grouping "
    "key from '_id' to a descriptive field name (e.g. grouping by vendor_id -> output field "
    '"vendor_id", not "_id"). The rows your query returns are the ONLY thing used later to '
    'write the final answer -- that step never sees your pipeline, so an opaque "_id" will not '
    'be understood as "vendor_id" or whatever you actually grouped by, and will read as if the '
    "data is missing or unrelated.\n"
    "{geo_rule}"
    "\nQuestion: {question}\n"
)

_GEO_FIELD_DOC = (
    ',\n    "geo_near": null,  // {"field": "location", "longitude": ..., "latitude": ..., '
    '"max_distance_m": ...} -- only if the question gives explicit coordinates\n'
    '    "requested_radius_m": null  // the distance the question asked for (e.g. 5000 for '
    '"within 5km"), if any -- set this even when geo_near itself must stay null'
)

_GEO_RULE = (
    "- geo_near: only set this if the question gives you explicit latitude/longitude "
    'coordinates. If it instead names a place, a person, or an id (e.g. "near Karachi" or '
    '"near customer USR-00001") and you don\'t have real coordinates for it, leave geo_near '
    "null and filter/reference that entity by its identifying field instead -- do not invent "
    "coordinates. Still set requested_radius_m if the question gave a distance.\n"
)


def generate_domain_query_spec(
    gemini: GeminiClient, domain: DomainConfig, question: str
) -> QuerySpec | QueryError:
    schema_context = build_domain_schema_context(domain.collection, domain.schema_fields)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = _PROMPT.format(
        today=today,
        collection=domain.collection,
        domain=domain.name,
        schema_context=schema_context,
        question=question,
        geo_field_doc=_GEO_FIELD_DOC if domain.geo_capable else "",
        geo_rule=_GEO_RULE if domain.geo_capable else "",
    )
    result = gemini.generate_structured_or_error(prompt, QuerySpec)
    if isinstance(result, QueryError):
        return result
    return scope_spec_to_domain(result, domain)
