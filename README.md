# India Market Brief

Live headlines from Business Standard, SCMP and Al Jazeera, scored for India impact. Flask backend, Tailwind CDN frontend, bookmarks saved in browser LocalStorage (no database).

## Structure

```
app.py
requirements.txt
templates/index.html
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py        # http://localhost:5000
```

## Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## Deploy on Render (free)

1. Render dashboard → New → Web Service → connect the GitHub repo.
2. Settings:
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --workers 2 --threads 4 --timeout 60`
   - Instance Type: Free
3. Deploy. Open the `.onrender.com` URL on Android, then Chrome menu → Add to Home screen.

Every push to `main` redeploys automatically.

## Keep it awake

The free tier sleeps after about 15 minutes without traffic, and the first request afterwards takes roughly 30-60 seconds. To avoid that, add a free UptimeRobot monitor that pings `https://<your-app>.onrender.com/healthz` every 5 minutes.

## API

`GET /api/news?count=10` where count is one of 5, 10, 15, 20, 25, 30. Returns `gauges` and `slides`. Results are cached for 5 minutes.

## How scoring works

- Ingestion: RSS feeds per source. If a source returns nothing, it falls back to a Google News RSS query restricted to that site.
- Impact ranking: theme weights (RBI, Nifty/Sensex, crude, rupee, Fed, geopolitics and others), headline sentiment strength, source bias and recency. The top N by impact become the slides, so a smaller N keeps only the highest-impact stories.
- Sentiment and probability: keyword lexicon, mapped to 50 ± 45%. Crude-oil headlines are inverted, since higher oil is bearish for India.
- Gauges: impact-weighted average over all fetched items tagged to Economy, Market or Global, independent of slide count.
- Colors: green at 58% or above, red at 42% or below, yellow in between.

This is a heuristic keyword model, not investment advice. To swap in an LLM or news API, replace `score()` in `app.py`.
