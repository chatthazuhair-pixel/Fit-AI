# FitAI

AI-assisted nutrition and home-workout coach. FastAPI + SQLite + Gemini API.

## Run locally
```
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export GEMINI_API_KEY=your_key                      # Windows: set GEMINI_API_KEY=your_key
uvicorn main:app --reload
```
Open http://127.0.0.1:8000

## Design
- Calories come from a nutrition table, never from the AI's memory. Gemini only reads meal text and writes coaching text.
- Workouts are rules-based. Gemini never invents exercises.
- Per-user and global daily AI caps protect the free quota.
- Data older than RETENTION_DAYS is deleted at startup. Users can export or delete their data.
- General guidance only, not medical advice.

## Known limits
- Starter nutrition table is small and approximate. Verify against USDA FoodData Central.
- Anonymous per-browser ID, no real accounts yet.
