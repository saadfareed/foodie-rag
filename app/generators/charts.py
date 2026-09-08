"""Pick and render a chart for a table, without touching pyplot.

Two things matter here.

**Thread safety.** `matplotlib.pyplot` keeps a process-global figure registry, so two Socket Mode
worker threads rendering reports at the same time can have `plt.subplots()`/`plt.close()`
interleave and corrupt each other's state. Every function below uses the object-oriented
`Figure` API instead, which owns no global state -- the only safe way to render from a thread
pool.

**Chart choice is rule-based, not model-chosen.** Asking Gemini which chart to draw would add a
whole round trip to the most quota-constrained path in the system for a decision that follows
mechanically from the data's shape: a small set of categories summing to a whole is a pie, a
time series is a line, everything else is a bar. The rules live in `choose_chart` and are
readable and testable on their own.
"""

import base64
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from matplotlib.figure import Figure

from app.config import settings
from app.generators.tabular import ReportTable

ChartKind = Literal["pie", "bar", "line"]

# Colour-blind-safe qualitative palette (Okabe-Ito), used in order. A report is often printed or
# read on a projector, so hue alone should never be the only thing separating two slices.
_PALETTE = (
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#D55E00",
    "#CC79A7",
    "#56B4E9",
    "#F0E442",
    "#7F7F7F",
)
_TEXT_COLOR = "#333333"
_GRID_COLOR = "#DDDDDD"


@dataclass(frozen=True)
class ChartSpec:
    """What to draw, resolved from the table's shape."""

    kind: ChartKind
    label_column: int
    value_column: int
    title: str


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_temporal(value: Any) -> bool:
    return isinstance(value, (datetime, date))


def _column_values(table: ReportTable, index: int) -> list[Any]:
    return [row[index] for row in table.rows if index < len(row)]


def _numeric_column_indexes(table: ReportTable) -> list[int]:
    """Columns where every non-null value is a number. A column of mostly-numbers with one
    stray string is not a measure -- it's an id or a status code that happens to look numeric."""
    result = []
    for i in range(len(table.headers)):
        values = [v for v in _column_values(table, i) if v is not None]
        if values and all(_is_number(v) for v in values):
            result.append(i)
    return result


# Words that name a column without using its column name. A question filtered to "incomplete"
# orders is a question *about status*, and the chart that adds most to that report is the one
# showing where those orders actually are -- which stage they're stuck at, not how they're paid.
# Without this the dimension falls to whichever column happens to have fewest distinct values.
#
# Keyed by the column name each group implies; extend rather than generalize, since the value
# here is in the specific vocabulary a domain actually uses.
_DIMENSION_SYNONYMS: dict[str, frozenset[str]] = {
    "status": frozenset(
        {
            "incomplete",
            "complete",
            "completed",
            "pending",
            "outstanding",
            "open",
            "unfinished",
            "stuck",
            "state",
            "stage",
            "progress",
            "cancelled",
            "delivered",
            "refunded",
        }
    ),
}


# Header words too generic to tell one column from another. "Order Type" and "Order Payment"
# both contain "order", and so does almost every question about orders -- matching on it would
# mark every column as mentioned and collapse this back to a pure cardinality tie-break.
_GENERIC_HEADER_WORDS = frozenset(
    {
        "order",
        "orders",
        "customer",
        "customers",
        "vendor",
        "vendors",
        "detail",
        "details",
        "data",
        "record",
        "records",
        "name",
        "current",
    }
)


def _mentioned_in(question: str, header: str) -> bool:
    """Whether the question refers to this column, by word or by an implying synonym.

    Word-level matching, not substring: "status" should match "Status" and "order status", while
    "city" must not match a question mentioning "capacity".
    """
    words = set(re.findall(r"[a-z]+", question.lower()))
    header_words = [
        w
        for w in re.findall(r"[a-z]+", header.lower())
        if len(w) > 2 and w not in _GENERIC_HEADER_WORDS
    ]
    if header_words and any(word in words for word in header_words):
        return True
    for column, synonyms in _DIMENSION_SYNONYMS.items():
        if column in {w.lower() for w in header_words} and words & synonyms:
            return True
    return False


def _label_column_index(
    table: ReportTable, exclude: set[int], question: str | None = None
) -> int | None:
    """The column that best works as a chart's dimension.

    Picking the *first* non-numeric column is the obvious rule and it is wrong on real data. A
    raw `orders` result leads with `order_id`, which is unique per row -- charting against it
    produces one bar per order and communicates nothing.

    Ranking is:

    1. **A column the question actually named.** Someone who asked for "orders by status" wants
       a chart by status, even when `city` happens to have fewer distinct values. Cardinality is
       a fallback heuristic; the question is direct evidence, so it wins.
    2. **Lowest cardinality** among the rest -- low cardinality is what makes a column a
       dimension rather than a key.

    Constant columns are excluded rather than ranked first. They are the lowest cardinality of
    all and the least useful: a `city` column reading "Karachi" on every row would win on score
    and collapse the chart to a single bar. A dimension needs at least two values to divide the
    measure between.
    """
    candidates = []
    for i in range(len(table.headers)):
        if i in exclude:
            continue
        values = [v for v in _column_values(table, i) if v is not None]
        if not values:
            continue
        distinct = len({str(v) for v in values})
        if distinct < 2:
            continue
        mentioned = bool(question) and _mentioned_in(question, table.headers[i])
        # Sort key: mentioned columns first, then fewest distinct values, then leftmost.
        candidates.append((0 if mentioned else 1, distinct, i))

    if not candidates:
        return None
    return min(candidates)[2]


def extract_series(table: ReportTable, spec: "ChartSpec") -> list[tuple[Any, float]]:
    """`(label, value)` pairs for the chart, summed per distinct label.

    Aggregation is why this is shared between `choose_chart` and `render_chart_png` rather than
    living in the renderer. Raw (un-grouped) rows repeat their dimension -- twenty orders across
    three statuses -- and plotting them unaggregated would draw twenty slices for three
    categories. Summing first means the pie-vs-bar threshold is judged on what will actually be
    drawn, not on the row count.
    """
    totals: dict[str, float] = {}
    labels: dict[str, Any] = {}
    for row in table.rows:
        if spec.label_column >= len(row) or spec.value_column >= len(row):
            continue
        value = row[spec.value_column]
        if not _is_number(value):
            continue
        label = row[spec.label_column]
        key = str(label)
        totals[key] = totals.get(key, 0) + float(value)
        labels.setdefault(key, label)
    return [(labels[key], total) for key, total in totals.items()]


def choose_chart(table: ReportTable, question: str | None = None) -> ChartSpec | None:
    """Resolve a chart from the table's shape, or None when no chart would say anything.

    A chart is only worth drawing when there is a measure to plot and a label to plot it
    against. A single row has nothing to compare, and a table with no numeric column has nothing
    to measure -- in both cases the table itself is the better representation, and an empty or
    one-bar chart is just noise on the page.

    `question` is optional context used only to prefer a dimension the user actually named --
    see _label_column_index. Everything else is decided from the data's shape alone.
    """
    if len(table.rows) < 2 or not table.headers:
        return None

    numeric_indexes = _numeric_column_indexes(table)
    if not numeric_indexes:
        return None

    value_column = numeric_indexes[0]
    label_column = _label_column_index(table, set(numeric_indexes), question)
    if label_column is None:
        return None

    measure = table.headers[value_column]
    dimension = table.headers[label_column]
    raw_labels = _column_values(table, label_column)

    if raw_labels and all(_is_temporal(v) for v in raw_labels if v is not None):
        return ChartSpec("line", label_column, value_column, f"{measure} over time")

    bar = ChartSpec("bar", label_column, value_column, f"{measure} by {dimension}")
    series = extract_series(table, bar)
    if len(series) < 2:
        # After aggregation there is a single category -- one bar comparing nothing.
        return None

    # A pie has to represent parts of a whole: negative or zero values have no slice, and too
    # many slices are unreadable, so both fall back to bars.
    if len(series) <= settings.report_max_pie_slices and all(value > 0 for _, value in series):
        return ChartSpec("pie", label_column, value_column, f"{measure} by {dimension}")
    return bar


def _style_axes(ax) -> None:
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(_GRID_COLOR)
    ax.tick_params(colors=_TEXT_COLOR, labelsize=9)
    ax.title.set_color(_TEXT_COLOR)


def _truncate_label(value: Any, limit: int = 22) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_chart_png(table: ReportTable, spec: ChartSpec) -> bytes:
    """Render `spec` for `table` as PNG bytes, using only thread-local Figure state."""
    pairs = extract_series(table, spec)
    if not pairs:
        raise ValueError("no plottable pairs in table")

    figure = Figure(figsize=(8, 4.5), dpi=140)
    figure.patch.set_facecolor("white")
    ax = figure.subplots()

    if spec.kind == "pie":
        labels = [_truncate_label(label) for label, _ in pairs]
        values = [value for _, value in pairs]
        wedges, _texts, autotexts = ax.pie(
            values,
            labels=labels,
            autopct="%1.1f%%",
            colors=_PALETTE[: len(values)],
            startangle=90,
            counterclock=False,
            wedgeprops={"edgecolor": "white", "linewidth": 1.5},
            textprops={"fontsize": 9, "color": _TEXT_COLOR},
        )
        for autotext in autotexts:
            autotext.set_color("white")
            autotext.set_fontsize(8)
        ax.set_aspect("equal")
    elif spec.kind == "line":
        pairs.sort(key=lambda p: p[0])
        ax.plot(
            [label for label, _ in pairs],
            [value for _, value in pairs],
            color=_PALETTE[0],
            linewidth=2,
            marker="o",
            markersize=4,
        )
        ax.grid(True, axis="y", color=_GRID_COLOR, linewidth=0.8)
        ax.set_axisbelow(True)
        figure.autofmt_xdate(rotation=30, ha="right")
        _style_axes(ax)
    else:
        # Horizontal bars: category labels are usually long, and horizontal keeps them readable
        # without rotating text. Descending, so the largest contributor is the top line.
        top = sorted(pairs, key=lambda p: p[1], reverse=True)[: settings.report_max_pie_slices * 2]
        top.reverse()
        labels = [_truncate_label(label) for label, _ in top]
        values = [value for _, value in top]
        ax.barh(labels, values, color=_PALETTE[0], height=0.65)
        ax.grid(True, axis="x", color=_GRID_COLOR, linewidth=0.8)
        ax.set_axisbelow(True)
        _style_axes(ax)

    ax.set_title(spec.title, fontsize=12, pad=12, color=_TEXT_COLOR)
    figure.tight_layout()

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", facecolor=figure.get_facecolor())
    return buffer.getvalue()


def render_chart_base64(table: ReportTable, question: str | None = None) -> tuple[str, str] | None:
    """`(base64_png, chart_title)` for the table, or None when no chart suits it."""
    spec = choose_chart(table, question)
    if spec is None:
        return None
    try:
        png = render_chart_png(table, spec)
    except (ValueError, TypeError):
        # A chart is a nice-to-have on a report whose table is the actual answer -- a plotting
        # failure on odd data must not cost the user the whole document.
        return None
    return base64.b64encode(png).decode("ascii"), spec.title
