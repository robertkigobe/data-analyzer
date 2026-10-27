"""Gradio web UI for the Data Analyzer (runs as a Hugging Face Space).

Run locally:   .venv/bin/python app.py      then open http://127.0.0.1:7860
"""

import os
import time
from collections import defaultdict

import gradio as gr
from dotenv import load_dotenv

load_dotenv()  # load ANTHROPIC_API_KEY etc. from .env before importing core

import core  # noqa: E402

# Cost controls. Each question costs roughly $0.001 on Claude Haiku 5.5, so the
# global cap below limits the worst case to well under $1/day.
DAILY_QUESTION_LIMIT = int(os.environ.get("DAILY_QUESTION_LIMIT", "10"))    # per visitor
GLOBAL_DAILY_LIMIT = int(os.environ.get("GLOBAL_DAILY_LIMIT", "300"))       # all visitors
MAX_FILE_SIZE = os.environ.get("MAX_FILE_SIZE", "50mb")
NUM_AUTO_CHARTS = 4

EXAMPLE_QUESTIONS = [
    "Which category has the highest total?",
    "Show the monthly trend",
    "Find the 10 most expensive items",
    "Compare the average across groups",
]

# Questions asked per (visitor, day), plus a "*" total for everyone. Kept in
# memory: resets when the Space restarts, which is fine for a free-tier limit.
_usage = defaultdict(int)


def _client_ip(request: gr.Request) -> str:
    # Hugging Face (and nginx) sit in front of the app, so the direct client is
    # a proxy. The visitor's address is the first entry in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or request.client.host


def _today() -> str:
    return time.strftime("%Y-%m-%d")


# ── Event handlers ──────────────────────────────────────────────────────────

def on_upload(file):
    """Load the file, then show an overview and starter charts (no AI used)."""
    empty_plots = [gr.Plot(value=None, visible=False) for _ in range(NUM_AUTO_CHARTS)]
    if file is None:
        return [None, "", None, None] + empty_plots

    try:
        df = core.load_dataframe(file)
    except Exception as e:
        return [None, f"⚠️ Could not read the file: {e}", None, None] + empty_plots

    status = f"✅ Loaded **{len(df):,} rows** and **{len(df.columns)} columns**."
    if "total_rows" in df.attrs:
        status += (f" ⚠️ Your file has {df.attrs['total_rows']:,} rows; this free tool "
                   f"analyses the first {core.MAX_ROWS:,}.")

    figs = core.auto_charts(df, NUM_AUTO_CHARTS)
    plots = [gr.Plot(value=f, visible=True) for f in figs]
    plots += [gr.Plot(value=None, visible=False) for _ in range(NUM_AUTO_CHARTS - len(figs))]

    return [df, status, core.column_overview(df), df.head(20)] + plots


def _message(text):
    return text, gr.Plot(visible=False), gr.DataFrame(visible=False)


def on_ask(df, question, request: gr.Request):
    """Send the question (with a summary of the data) to Claude, then apply its
    answer to the full data."""
    if df is None:
        return _message("Please upload a dataset first.")
    if not question or not question.strip():
        return _message("Please type a question.")

    visitor = (_client_ip(request), _today())
    everyone = ("*", _today())
    if _usage[everyone] >= GLOBAL_DAILY_LIMIT:
        return _message("The free AI questions for today have all been used. Please come back tomorrow!")
    if _usage[visitor] >= DAILY_QUESTION_LIMIT:
        return _message(f"You've used today's {DAILY_QUESTION_LIMIT} free AI questions. Come back tomorrow!")

    try:
        spec = core.ask_claude(df, question.strip()[:500])
        _usage[visitor] += 1
        _usage[everyone] += 1
        fig, table = core.build_chart(df, spec)
    except Exception as e:
        return _message(f"⚠️ {e}")

    left = DAILY_QUESTION_LIMIT - _usage[visitor]
    answer = f"{spec.answer}\n\n<sub>{left} AI questions left today.</sub>"
    return (
        answer,
        gr.Plot(value=fig, visible=fig is not None),
        gr.DataFrame(value=table, visible=table is not None),
    )


# ── Layout ──────────────────────────────────────────────────────────────────

# delete_cache: every 30 min, remove uploaded files older than 1 hour.
with gr.Blocks(title="Data Analyzer · kigobe.net", delete_cache=(1800, 3600)) as demo:
    df_state = gr.State(None)

    gr.Markdown(
        "# 📊 Data Analyzer\n"
        "Upload a CSV or Excel file to see instant charts, then ask questions in plain English - "
        "find rows, compare groups, or see trends.\n\n"
        "<sub>Uploaded files are deleted within an hour. Only a short summary "
        "(column names, simple statistics and 5 sample rows) is sent to the AI. "
        "By [Robert Kigobe](https://kigobe.net).</sub>"
    )

    file_in = gr.File(label="Upload dataset (.csv or .xlsx)", file_types=[".csv", ".xlsx"], type="filepath")
    status = gr.Markdown()

    with gr.Tab("Ask a question"):
        with gr.Row():
            question = gr.Textbox(label="Your question", placeholder="e.g. Which region had the highest sales?", scale=4)
            ask_btn = gr.Button("Ask", variant="primary", scale=1)
        gr.Examples(EXAMPLE_QUESTIONS, inputs=question)
        answer = gr.Markdown()
        answer_plot = gr.Plot(visible=False)
        answer_table = gr.DataFrame(label="Result (computed from the full data)", visible=False)

    with gr.Tab("Overview"):
        with gr.Row():
            auto_plots = [gr.Plot(visible=False) for _ in range(2)]
        with gr.Row():
            auto_plots += [gr.Plot(visible=False) for _ in range(2)]
        overview = gr.DataFrame(label="Columns")
        preview = gr.DataFrame(label="First 20 rows")

    file_in.change(on_upload, file_in, [df_state, status, overview, preview] + auto_plots)
    ask_inputs, ask_outputs = [df_state, question], [answer, answer_plot, answer_table]
    ask_btn.click(on_ask, ask_inputs, ask_outputs)
    question.submit(on_ask, ask_inputs, ask_outputs)


# Limit how many jobs run at once so a busy moment can't overload the free CPU.
demo.queue(default_concurrency_limit=2, max_size=20)

if __name__ == "__main__":
    # Host/port come from GRADIO_SERVER_NAME / GRADIO_SERVER_PORT when set
    # (Hugging Face sets them); otherwise Gradio's local defaults apply.
    demo.launch(
        max_file_size=MAX_FILE_SIZE,
        ssr_mode=False,
        theme=gr.themes.Soft(primary_hue="amber"),
    )
