"""Core logic for the Data Analyzer: load data, summarise it, ask Claude, draw charts.

Safety rule: Claude never writes code that we run. It only returns a small
AnalysisSpec (filters, chart type, column names), and our own code applies it
to the full data with pandas.
"""

import os
from pathlib import Path
from typing import List, Literal, Optional

import anthropic
import pandas as pd
import plotly.express as px
from pydantic import BaseModel, Field

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
MAX_ROWS = int(os.environ.get("MAX_ROWS", "5000"))          # rows analysed, after cleaning
READ_LIMIT = int(os.environ.get("READ_LIMIT", "200000"))   # raw rows read (exports are often mostly blank)
SAMPLE_ROWS = 5
MAX_TABLE_ROWS = 100

_client = None


def get_client() -> anthropic.Anthropic:
    """Create the Claude client on first use (reads ANTHROPIC_API_KEY from the
    environment), so the app can start and show charts even without a key."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ── Loading and cleaning ────────────────────────────────────────────────────

def load_dataframe(path: str) -> pd.DataFrame:
    """Read a CSV or Excel file into a clean DataFrame of at most MAX_ROWS rows.
    If rows were dropped, df.attrs["total_rows"] holds the original count."""
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        try:
            df = pd.read_csv(path, nrows=READ_LIMIT, low_memory=False)
        except UnicodeDecodeError:
            # Files exported from Excel on Windows are often cp1252, not UTF-8.
            df = pd.read_csv(path, nrows=READ_LIMIT, low_memory=False, encoding="cp1252")
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, nrows=READ_LIMIT)
    else:
        raise ValueError("Please upload a .csv or .xlsx file.")

    df = clean_dataframe(df)
    if len(df) > MAX_ROWS:
        total = len(df)
        df = df.head(MAX_ROWS).copy()
        df.attrs["total_rows"] = total
    return df


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Fix the usual spreadsheet mess: blank rows and columns, money stored as
    text ("$1,234.50"), and dates stored as text."""
    df.columns = [str(c).strip() for c in df.columns]

    # Excel exports often contain thousands of rows/columns of nothing - or rows
    # that are empty apart from values copied down a column. Keep rows that
    # have at least half their cells filled in.
    df = df.replace(r"^\s*$", pd.NA, regex=True)
    df = df.dropna(axis=1, how="all")
    df = df.loc[:, ~df.columns.str.startswith("Unnamed:")]
    df = df.dropna(thresh=(len(df.columns) + 1) // 2)
    if df.empty:
        raise ValueError("The file has no data rows.")

    for col in df.select_dtypes(exclude=["number", "datetime"]).columns:
        values = df[col].dropna().astype(str).str.strip()
        if values.empty:
            continue
        if values.str.fullmatch(r"[-+]?\s*[$€£R]?\s*[\d,\s]*\d(\.\d+)?").mean() > 0.9:
            numbers = pd.to_numeric(df[col].astype(str).str.replace(r"[$€£R,\s]", "", regex=True), errors="coerce")
            if numbers.notna().sum() >= 0.9 * len(values):
                df[col] = numbers
                continue
        if values.head(50).str.match(r"^\d{4}-\d{1,2}-\d{1,2}|^\d{1,2}/\d{1,2}/\d{2,4}").all():
            parsed = pd.to_datetime(df[col], errors="coerce")
            if parsed.notna().mean() > 0.9:
                df[col] = parsed

    return df.reset_index(drop=True)


# ── Summaries (no AI needed) ────────────────────────────────────────────────

def column_overview(df: pd.DataFrame) -> pd.DataFrame:
    """One row per column: type, missing values, unique values, example."""
    return pd.DataFrame({
        "column": df.columns,
        "type": [str(t) for t in df.dtypes],
        "missing": df.isna().sum().values,
        "unique": df.nunique().values,
        "example": [str(df[c].dropna().iloc[0]) if df[c].notna().any() else "" for c in df.columns],
    })


def summary_for_llm(df: pd.DataFrame) -> str:
    """A compact text description of the dataset. This is ALL Claude sees -
    never the full file - which keeps every question cheap whatever the file size."""
    lines = [f"Rows: {len(df)}, Columns: {len(df.columns)}", "", "Columns:"]
    for col in df.columns:
        s = df[col]
        line = f"- {col} ({s.dtype}), {s.isna().sum()} missing, {s.nunique()} unique"
        if pd.api.types.is_datetime64_any_dtype(s):
            line += f", from {s.min():%Y-%m-%d} to {s.max():%Y-%m-%d}"
        elif pd.api.types.is_numeric_dtype(s):
            line += f", min={s.min()}, mean={s.mean():.3g}, max={s.max()}"
        else:
            top = [str(v)[:60] for v in s.value_counts().head(5).index]
            line += f", top values: {top}"
        lines.append(line)
    lines += ["", f"First {SAMPLE_ROWS} rows:", df.head(SAMPLE_ROWS).to_csv(index=False)]
    return "\n".join(lines)


def auto_charts(df: pd.DataFrame, limit: int = 4) -> list:
    """Starter charts for the first few columns: histograms for numbers,
    bar charts of the most common values for text columns."""
    figs = []
    numeric = df.select_dtypes("number").columns.tolist()
    categorical = [c for c in df.columns if c not in numeric and 1 < df[c].nunique() <= 50]

    for col in numeric[:2]:
        figs.append(px.histogram(df, x=col, title=f"Distribution of {col}"))
    for col in categorical[:2]:
        counts = df[col].value_counts().head(15).reset_index()
        counts.columns = [col, "count"]
        figs.append(px.bar(counts, x=col, y="count", title=f"Most common {col}"))

    return figs[:limit]


# ── Asking Claude ───────────────────────────────────────────────────────────

class Filter(BaseModel):
    column: str
    op: Literal["contains", "equals", "greater_than", "less_than"]
    value: str


class AnalysisSpec(BaseModel):
    """What Claude returns. Our code applies it to the full data."""
    answer: str = Field(description="A short plain-English answer or explanation for the user.")
    filters: List[Filter] = Field(default_factory=list, description="Row filters applied before anything else.")
    chart_type: Literal["table", "bar", "line", "scatter", "histogram", "box", "pie", "none"]
    x: Optional[str] = Field(default=None, description="Column for the x-axis (or pie labels).")
    y: Optional[str] = Field(default=None, description="Numeric column for the y-axis (or pie values).")
    color: Optional[str] = Field(default=None, description="Optional column to colour/group by.")
    aggregation: Literal["sum", "mean", "median", "count", "min", "max", "none"] = "none"
    time_unit: Literal["day", "week", "month", "year", "none"] = Field(
        default="none", description="When x is a date column: group dates into this unit.")
    sort_by: Optional[str] = Field(default=None, description="For tables: column to sort by.")
    sort_ascending: bool = True
    limit: int = Field(default=20, description="For tables: maximum rows to show.")
    title: str = ""


SYSTEM_PROMPT = """You help people explore a dataset they uploaded.
You only see a summary of the data (column names, types, simple statistics, a few sample rows), not the full file.
Use only column names that exist exactly as written in the summary.

Choose how to answer:
- To FIND specific rows (look up an ID or part number, "what X do we have", "cheapest/most expensive X"), use chart_type "table" with filters, and sort_by/sort_ascending/limit as needed. For "cheapest", sort ascending by the price column with a small limit.
- To COMPARE groups, use "bar" with x = group column, y = number column and an aggregation (sum, mean, ...).
- For TRENDS over time, use "line" with x = the date column, an aggregation, and a time_unit (e.g. "month" for a monthly trend).
- Use aggregation "count" (and no y) to count rows per group.
- Use "none" if the question can't be answered from these columns.

Filters narrow the rows first and work with every chart type. Use "contains" (case-insensitive) for searching text, "equals" for exact IDs, and greater_than/less_than for numbers.

In "answer", explain briefly what you're showing. Don't invent specific numbers that aren't in the summary - the chart and result table show the real values computed from the full data."""


def ask_claude(df: pd.DataFrame, question: str) -> AnalysisSpec:
    response = get_client().messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},  # a simple task: keep thinking (and cost) low
        output_format=AnalysisSpec,
        messages=[{
            "role": "user",
            "content": f"Dataset summary:\n{summary_for_llm(df)}\n\nQuestion: {question}",
        }],
    )
    if response.stop_reason == "refusal":
        raise ValueError("Sorry, I can't help with that question.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise ValueError("The AI response was incomplete. Please try rephrasing your question.")
    return response.parsed_output


# ── Applying an AnalysisSpec ────────────────────────────────────────────────

def _check_column(df: pd.DataFrame, name: Optional[str]) -> Optional[str]:
    if name is not None and name not in df.columns:
        raise ValueError(f"The AI suggested a column that doesn't exist: {name!r}")
    return name


def apply_filters(df: pd.DataFrame, filters: List[Filter]) -> pd.DataFrame:
    for f in filters:
        col = df[_check_column(df, f.column)]
        if f.op == "contains":
            mask = col.astype(str).str.contains(f.value, case=False, regex=False, na=False)
        elif f.op == "equals":
            mask = col.astype(str).str.strip().str.lower() == f.value.strip().lower()
        else:
            try:
                value = float(f.value.replace(",", "").lstrip("$€£R"))
            except ValueError:
                raise ValueError(f"Can't compare {f.column!r} with {f.value!r}.")
            numbers = pd.to_numeric(col, errors="coerce")
            mask = numbers > value if f.op == "greater_than" else numbers < value
        df = df[mask]
    return df


def build_chart(df: pd.DataFrame, spec: AnalysisSpec):
    """Turn an AnalysisSpec into (plotly figure or None, result table or None)."""
    df = apply_filters(df, spec.filters)
    if df.empty:
        raise ValueError("No rows match that question.")

    if spec.chart_type == "none":
        return None, None

    x = _check_column(df, spec.x)
    y = _check_column(df, spec.y)
    color = _check_column(df, spec.color)
    title = spec.title or None

    if spec.chart_type == "table":
        sort_by = _check_column(df, spec.sort_by)
        if sort_by:
            df = df.sort_values(sort_by, ascending=spec.sort_ascending)
        return None, df.head(max(1, min(spec.limit, MAX_TABLE_ROWS)))

    # Raw-data charts: no grouping needed.
    if spec.chart_type == "histogram":
        return px.histogram(df, x=x or y, color=color, title=title), None
    if spec.chart_type == "box":
        return px.box(df, x=x, y=y, color=color, title=title), None
    if spec.chart_type == "scatter":
        return px.scatter(df.head(5000), x=x, y=y, color=color, title=title), None

    # Grouped charts (bar / line / pie): aggregate first, so the result table
    # shows exactly the numbers that were plotted.
    if x is None:
        raise ValueError("The AI didn't pick a column to group by. Try rephrasing.")
    if spec.time_unit != "none" and pd.api.types.is_datetime64_any_dtype(df[x]):
        period = {"day": "D", "week": "W", "month": "M", "year": "Y"}[spec.time_unit]
        df = df.assign(**{x: df[x].dt.to_period(period).dt.start_time})
    group_cols = [x] + ([color] if color and color != x else [])

    if spec.aggregation == "count" or y is None:
        table = df.groupby(group_cols).size().reset_index(name="count")
        value = "count"
    elif spec.aggregation == "none":
        table = df[group_cols + [y]].dropna()
        value = y
    else:
        table = df.groupby(group_cols)[y].agg(spec.aggregation).reset_index()
        value = y

    if spec.chart_type == "line":
        table = table.sort_values(x)
        fig = px.line(table, x=x, y=value, color=color, title=title, markers=True)
    elif spec.chart_type == "pie":
        table = table.sort_values(value, ascending=False).head(12)
        fig = px.pie(table, names=x, values=value, title=title)
    else:
        table = table.sort_values(value, ascending=False).head(30)
        fig = px.bar(table, x=x, y=value, color=color, title=title)

    return fig, table
