"""PDF export: a fixed report template rendered through Jinja2 + WeasyPrint.

The template is deliberately fixed rather than model-authored. Every report gets the same four
blocks in the same order, so a reader learns the layout once:

1. **Title block** -- report title, the question that produced it, and a UTC timestamp.
2. **Key insights** -- the synthesized natural-language answer. This is reused from the answer
   the pipeline already generated, not a second Gemini call: the insight and the on-screen reply
   should say the same thing, and a separate call would both cost quota and risk them disagreeing.
3. **Visual** -- one chart, chosen from the data's shape by app/generators/charts.py.
4. **Data table** -- one section per domain, with an explicit note when rows or columns were
   capped.

Everything is embedded (the chart as a data: URI, all CSS inline), so the PDF is self-contained
and WeasyPrint never reaches the network while rendering.
"""

import html
import os
import tempfile
from datetime import datetime, timezone

# WeasyPrint and matplotlib both want a writable cache/config directory and fall back to complaining
# on stderr when HOME is unset (a container running as a non-root uid). Set before either import.
if "MPLCONFIGDIR" not in os.environ:
    os.environ["MPLCONFIGDIR"] = tempfile.gettempdir()

from jinja2 import Environment, select_autoescape  # noqa: E402
from weasyprint import HTML  # noqa: E402

from app.config import settings  # noqa: E402
from app.generators.charts import render_chart_base64  # noqa: E402
from app.generators.tabular import ReportTable, build_tables, table_note  # noqa: E402

_TEMPLATE_SOURCE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<style>
  @page {
    size: A4;
    margin: 18mm 15mm 20mm 15mm;
    @bottom-center {
      content: "Page " counter(page) " of " counter(pages);
      font-family: Helvetica, Arial, sans-serif;
      font-size: 8pt;
      color: #999;
    }
  }
  body { font-family: Helvetica, Arial, sans-serif; color: #333; font-size: 10pt; }
  .title-block { border-bottom: 3px solid #1F4E79; padding-bottom: 10px; margin-bottom: 18px; }
  h1 { color: #1F4E79; font-size: 20pt; margin: 0 0 6px 0; }
  .question { font-size: 10.5pt; color: #444; font-style: italic; margin: 0 0 4px 0; }
  .meta { font-size: 8.5pt; color: #888; margin: 0; }
  h2 {
    color: #1F4E79; font-size: 12.5pt; margin: 20px 0 8px 0;
    border-bottom: 1px solid #DDD; padding-bottom: 4px;
  }
  .insights { background: #F4F8FC; border-left: 4px solid #1F4E79; padding: 10px 14px;
              font-size: 10pt; line-height: 1.5; }
  .insights p { margin: 0 0 6px 0; }
  .insights p:last-child { margin-bottom: 0; }
  .chart { text-align: center; margin: 6px 0 4px 0; }
  .chart img { max-width: 100%; height: auto; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 6px; font-size: 8.5pt; }
  thead { display: table-header-group; }
  th {
    background: #1F4E79; color: #FFF; text-align: left;
    padding: 6px 7px; font-weight: bold; border: 1px solid #1F4E79;
  }
  td { padding: 5px 7px; border: 1px solid #DDD; }
  tbody tr:nth-child(even) { background: #F7F9FB; }
  .note { font-size: 8pt; color: #888; font-style: italic; margin: 0 0 14px 0; }
  .empty { color: #888; font-style: italic; }
</style>
</head>
<body>
  <div class="title-block">
    <h1>{{ title }}</h1>
    {% if question %}<p class="question">{{ question }}</p>{% endif %}
    <p class="meta">Generated {{ generated_at }}</p>
  </div>

  {% if insight_paragraphs %}
  <h2>Key Insights</h2>
  <div class="insights">
    {% for paragraph in insight_paragraphs %}<p>{{ paragraph }}</p>{% endfor %}
  </div>
  {% endif %}

  {% if chart_base64 %}
  <h2>{{ chart_title }}</h2>
  <div class="chart">
    <img src="data:image/png;base64,{{ chart_base64 }}" alt="{{ chart_title }}" />
  </div>
  {% endif %}

  <h2>Data</h2>
  {% if not tables %}
    <p class="empty">No data matched this question.</p>
  {% endif %}
  {% for table in tables %}
    <h3>{{ table.title }}</h3>
    <table>
      <thead><tr>{% for header in table.headers %}<th>{{ header }}</th>{% endfor %}</tr></thead>
      <tbody>
        {% for row in table.rows %}
        <tr>{% for cell in row %}<td>{{ cell if cell is not none else "" }}</td>{% endfor %}</tr>
        {% endfor %}
      </tbody>
    </table>
    {% if table.note %}<p class="note">{{ table.note }}</p>{% endif %}
  {% endfor %}
</body>
</html>
"""

# autoescape is the point of using an Environment here rather than a bare Template: every cell
# below is database content, and an unescaped "<" in a business name would corrupt the document
# (or inject markup into it).
_ENVIRONMENT = Environment(autoescape=select_autoescape(default_for_string=True))
_TEMPLATE = _ENVIRONMENT.from_string(_TEMPLATE_SOURCE)


def _insight_paragraphs(answer: str | None) -> list[str]:
    """Split the synthesized answer into paragraphs, stripping Markdown the PDF renders itself.

    The answer may contain a Markdown table (the Slack reply wants one). In the PDF that content
    is already presented properly in the Data section, so table lines are dropped rather than
    printed twice as raw pipes.
    """
    if not answer:
        return []
    paragraphs = []
    for block in answer.split("\n\n"):
        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("|") and set(line.strip()) != {"-"}
        ]
        if lines:
            paragraphs.append(html.unescape(" ".join(lines)))
    return paragraphs


def _pick_chart(tables: list[ReportTable], question: str | None) -> tuple[str, str] | None:
    """Chart the first table that yields one -- one visual per report, not one per domain."""
    for table in tables:
        rendered = render_chart_base64(table, question)
        if rendered is not None:
            return rendered
    return None


def generate_pdf(
    rows_by_domain: dict[str, list[dict]],
    question: str | None = None,
    answer: str | None = None,
    title: str | None = None,
) -> bytes:
    """Render the report template to PDF bytes."""
    tables = build_tables(rows_by_domain)
    chart = _pick_chart(tables, question)

    html_content = _TEMPLATE.render(
        title=title or settings.report_title,
        question=question,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        insight_paragraphs=_insight_paragraphs(answer),
        chart_base64=chart[0] if chart else None,
        chart_title=chart[1] if chart else None,
        tables=[
            {
                "title": table.title,
                "headers": table.headers,
                "rows": table.rows,
                "note": table_note(table),
            }
            for table in tables
        ],
    )
    return HTML(string=html_content).write_pdf()
