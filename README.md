---
title: Data Analyzer
emoji: 📊
colorFrom: yellow
colorTo: gray
sdk: gradio
sdk_version: 6.30.0
python_version: "3.12"
app_file: app.py
pinned: false
license: mit
short_description: Upload a spreadsheet, get charts, ask questions in English
---

# Data Analyzer

Upload a CSV or Excel file, get instant charts, then ask questions about the data in plain English:
find rows ("do we have part 0108141-1?"), compare groups ("total revenue by region"), or see trends ("monthly units sold").

- **Live app:** https://huggingface.co/spaces/rkigobe/data-analyzer
- **On kigobe.net:** https://kigobe.net/projects/data-analyzer
- **Source:** https://github.com/robertkigobe/data-analyzer (MIT licensed, contributions welcome)

## How it works

1. **Upload**: pandas loads and cleans the file (blank rows, money stored as text like `$1,234.50`, dates, Windows encodings), then draws starter charts. No AI is used, so this step is free.
2. **Ask**: only a short summary (column names, simple stats, 5 sample rows) is sent to Claude, so each question costs about the same whatever the file size.
3. **Answer**: Claude replies with a small `AnalysisSpec` (filters, chart type, column names). Our code applies it to the **full** data, so the numbers are computed, not guessed. **Claude never writes code that we run.**

| File | What it does |
|---|---|
| `core.py` | Loading/cleaning, summaries, the Claude call, applying the spec |
| `app.py` | Gradio UI, daily question limits, job queue |
| `deploy.sh` | Publishes to the Hugging Face Space |

## Running cheaply

| Lever | Setting |
|---|---|
| Hosting | Hugging Face free CPU tier ($0; sleeps when idle) |
| Model | `claude-haiku-5-5` at effort `low`, about $0.001 per question |
| Per visitor | `DAILY_QUESTION_LIMIT` = 10 AI questions per day |
| Everyone | `GLOBAL_DAILY_LIMIT` = 300 AI questions per day, so the worst case is well under $1/day |
| Input size | Summary only, never the full file. `MAX_FILE_SIZE` 50 MB; `MAX_ROWS` 5,000 rows analysed (after blank rows are removed) |

Also set a **monthly spend limit** in the Anthropic Console as a final safety net.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then put your ANTHROPIC_API_KEY in .env
.venv/bin/python app.py     # open http://127.0.0.1:7860
```

Try it with `sample_data/sales.csv`. Charts work without an API key; only "Ask a question" needs one.

## Deploy

```bash
./deploy.sh
```

The first time, add `ANTHROPIC_API_KEY` as a **secret** in the Space's Settings → Variables and secrets (never commit it).

To run your own copy, change `SPACE_REPO` in `deploy.sh` to your own Hugging Face username/space.

## Contributing

Issues and pull requests are welcome. Please keep the core safety rule: the model returns a
structured spec, and only our own code touches the data. Never `exec` model output.

## License

[MIT](LICENSE) © 2026 Robert Kigobe
