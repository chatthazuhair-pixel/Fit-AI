"""FitAI backend: FastAPI + SQLite + Gemini. Run: uvicorn main:app --reload"""
import os, json, re, sqlite3, datetime as dt, math
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal
from pydantic import BaseModel
from food_catalog import FOOD_CATALOG, FOOD_BY_ID, QUICK_FOODS, resolve_food

DB = os.getenv("DB_PATH", "fitai.db")
KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
USER_LIMIT = int(os.getenv("USER_DAILY_LIMIT", "10"))
GLOBAL_LIMIT = int(os.getenv("GLOBAL_DAILY_LIMIT", "200"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

app = FastAPI(title="FitAI")

# Generic reference nutrition values per 100 g.
# Food labels in food_catalog.py identify the preparation state.
NUTR = {
    food["id"]: (
        food["kcal_per_100g"], food["protein_per_100g"],
        food["carbs_per_100g"], food["fat_per_100g"],
    )
    for food in FOOD_CATALOG
}

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS profiles(uid TEXT PRIMARY KEY, goal TEXT, level TEXT, equipment TEXT, diet TEXT);
        CREATE TABLE IF NOT EXISTS food(id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, day TEXT, name TEXT, grams REAL, kcal REAL, protein REAL, carbs REAL, fat REAL);
        CREATE TABLE IF NOT EXISTS ai_usage(uid TEXT, day TEXT, n INTEGER, PRIMARY KEY(uid, day));
        CREATE TABLE IF NOT EXISTS workouts(id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, day TEXT);
        """)
        try: c.execute("ALTER TABLE profiles ADD COLUMN age_group TEXT")
        except sqlite3.OperationalError: pass

def cleanup():
    """Retention: delete data older than RETENTION_DAYS."""
    cutoff = (dt.date.today() - dt.timedelta(days=RETENTION_DAYS)).isoformat()
    with db() as c:
        for t in ("food", "ai_usage", "workouts"):
            c.execute(f"DELETE FROM {t} WHERE day < ?", (cutoff,))

init(); cleanup()
today = lambda: dt.date.today().isoformat()

def uid_ok(uid):
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", uid or ""):
        raise HTTPException(400, "Bad user id")
    return uid

def spend_ai(uid):
    """Per-user and global daily caps protect the free API quota."""
    with db() as c:
        total = c.execute("SELECT COALESCE(SUM(n),0) FROM ai_usage WHERE day=?", (today(),)).fetchone()[0]
        row = c.execute("SELECT n FROM ai_usage WHERE uid=? AND day=?", (uid, today())).fetchone()
        mine = row["n"] if row else 0
        if mine >= USER_LIMIT:
            raise HTTPException(429, f"Daily limit reached ({USER_LIMIT} AI requests). Try again tomorrow.")
        if total >= GLOBAL_LIMIT:
            raise HTTPException(429, "The app has reached its free AI quota for today.")
        c.execute("INSERT INTO ai_usage VALUES(?,?,1) ON CONFLICT(uid,day) DO UPDATE SET n=n+1", (uid, today()))

def gemini(prompt, system, json_mode=False):
    if not KEY:
        raise HTTPException(503, "GEMINI_API_KEY is not set on the server.")
    cfg = {"maxOutputTokens": 2048}
    if json_mode: cfg["responseMimeType"] = "application/json"
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": cfg}
    try:
        r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                       headers={"x-goog-api-key": KEY}, json=body, timeout=30)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print("Gemini error:", e)
        raise HTTPException(502, "The AI service is unavailable right now.")

def fallback_parse(text):
    """Parse simple meal descriptions with exact catalogue aliases only."""
    items = []
    parts = re.split(r"[,;\n]|\band\b", (text or "").casefold())
    factors = {
        "g": 1, "gram": 1, "grams": 1,
        "kg": 1000,
        "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
        "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
    }
    for part in parts:
        match = re.match(
            r"\s*(\d+(?:\.\d+)?)\s*(kg|g|grams?|oz|ounces?|lbs?|pounds?)?\s*(?:of\s+)?(.+?)\s*$",
            part,
        )
        if not match:
            continue
        try:
            quantity = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(quantity) or quantity <= 0:
            continue
        unit = match.group(2)
        name = match.group(3).strip(" .")
        food_id = resolve_food(name)
        if not food_id:
            continue
        grams = quantity * factors[unit] if unit else quantity * FOOD_BY_ID[food_id]["serving_g"]
        if math.isfinite(grams) and 0 < grams <= 2000:
            items.append({"name": food_id, "grams": grams})
    return items

class Profile(BaseModel):
    age_group: Literal["teen", "adult"]; goal: str; level: str; equipment: str; diet: str = ""
class Meal(BaseModel):
    text: str

class FoodEntry(BaseModel):
    food_id: str
    grams: float

class Ask(BaseModel):
    question: str

@app.post("/api/profile")
def save_profile(p: Profile, x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO profiles(uid,goal,level,equipment,diet,age_group) VALUES(?,?,?,?,?,?)",
                  (uid, p.goal[:30], p.level[:20], p.equipment[:60], p.diet[:60], p.age_group))
    return {"ok": True}

@app.get("/api/profile")
def get_profile(x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c:
        r = c.execute("SELECT goal,level,equipment,diet,age_group FROM profiles WHERE uid=?", (uid,)).fetchone()
    return dict(r) if r else {}

@app.get("/api/foods")
def list_foods():
    """Return searchable generic foods with nutrition values per 100 g."""
    return {
        "source_note": "Generic reference estimates per 100 g; brands, recipes and preparation vary.",
        "foods": [
            {key: value for key, value in food.items() if key != "aliases"} | {"quick": food["id"] in QUICK_FOODS}
            for food in FOOD_CATALOG
        ],
    }


def save_food_entry(uid, food_id, grams):
    food = FOOD_BY_ID[food_id]
    ratio = grams / 100.0
    kcal = food["kcal_per_100g"] * ratio
    protein = food["protein_per_100g"] * ratio
    carbs = food["carbs_per_100g"] * ratio
    fat = food["fat_per_100g"] * ratio
    with db() as c:
        c.execute(
            "INSERT INTO food(uid,day,name,grams,kcal,protein,carbs,fat) VALUES(?,?,?,?,?,?,?,?)",
            (uid, today(), food["name"], grams, kcal, protein, carbs, fat),
        )
    return {
        "id": food_id, "name": food["name"], "grams": round(grams, 1),
        "kcal": round(kcal, 1), "protein": round(protein, 1),
        "carbs": round(carbs, 1), "fat": round(fat, 1),
    }


@app.post("/api/food/item")
def log_food_item(entry: FoodEntry, x_user_id: str = Header(None)):
    """Log one exact catalogue item using a user-entered weight in grams."""
    uid = uid_ok(x_user_id)
    if entry.food_id not in FOOD_BY_ID:
        raise HTTPException(404, "That food is not in the catalogue.")
    if not math.isfinite(entry.grams) or not 0 < entry.grams <= 2000:
        raise HTTPException(422, "Enter a food weight greater than 0 and no more than 2,000 grams.")
    logged = save_food_entry(uid, entry.food_id, entry.grams)
    return {"logged": [logged], "not_in_database": []}


@app.post("/api/food")
def log_food(m: Meal, x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    text = m.text[:300]
    items = []
    if KEY:
        spend_ai(uid)
        prompt = (
            f"Meal description: {text}\n"
            "Return a JSON array of objects with exactly these fields: name and grams. "
            "The name MUST be one exact food ID from this catalogue: "
            + ", ".join(FOOD_BY_ID.keys())
            + ". Estimate the amount in grams only when a quantity is described. "
            "Do not invent a catalogue item and do not estimate nutrition. JSON only."
        )
        raw = gemini(
            prompt,
            "Extract food names and amounts only. Do not calculate calories or macros. "
            "Use only exact catalogue IDs supplied in the prompt.",
            json_mode=True,
        )
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                items = decoded
        except (TypeError, ValueError):
            items = []
    if not items:
        items = fallback_parse(text)

    logged, unknown = [], []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("name", "")).strip()
        food_id = resolve_food(raw_name)
        if not food_id:
            if raw_name:
                unknown.append(raw_name)
            continue
        try:
            grams = float(item.get("grams", 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(grams) or not 0 < grams <= 2000:
            continue
        logged.append(save_food_entry(uid, food_id, grams))
    return {"logged": logged, "not_in_database": unknown}

@app.get("/api/dashboard")
def dashboard(x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c:
        meals = [dict(r) for r in c.execute(
            "SELECT name,grams,kcal,protein,carbs,fat FROM food WHERE uid=? AND day=?", (uid, today()))]
        week = [dict(r) for r in c.execute(
            "SELECT day, ROUND(SUM(kcal)) kcal FROM food WHERE uid=? AND day>=? GROUP BY day ORDER BY day",
            (uid, (dt.date.today() - dt.timedelta(days=6)).isoformat()))]
        wk = c.execute("SELECT COUNT(*) FROM workouts WHERE uid=? AND day>=?",
                       (uid, (dt.date.today() - dt.timedelta(days=6)).isoformat())).fetchone()[0]
    s = lambda k: round(sum(m[k] for m in meals))
    return {"meals": meals, "totals": {"kcal": s("kcal"), "protein": s("protein"), "carbs": s("carbs"), "fat": s("fat")},
            "week": week, "workouts_this_week": wk}

PLANS = {
    "beginner": [("Chair squats", "2 x 8"), ("Wall push-ups", "2 x 8"),
                 ("Glute bridges", "2 x 10"), ("Standing calf raises", "2 x 10"),
                 ("Marching in place", "2 x 30 sec")],
    "intermediate": [("Bodyweight squats", "3 x 10"), ("Incline push-ups", "3 x 8"),
                     ("Reverse lunges", "2 x 8 each leg"), ("Glute bridges", "3 x 12"),
                     ("Marching in place", "3 x 1 min")],
    "advanced": [("Bodyweight squats", "3 x 12"), ("Push-ups", "3 x 10"),
                 ("Reverse lunges", "3 x 10 each leg"), ("Plank", "3 x 30 sec"),
                 ("Marching in place", "3 x 1 min")],
}
EXERCISE_GUIDES = {
    "Chair squats": "Stand in front of a stable chair, move your hips back, and lower only as far as comfortable. Use the chair for support if needed.",
    "Wall push-ups": "Place your hands on a wall at chest height. Keep your body straight and bend your elbows gently, then push back.",
    "Glute bridges": "Lie on your back with knees bent and feet flat. Lift your hips comfortably, pause briefly, and lower slowly.",
    "Standing calf raises": "Hold a stable surface for balance. Rise onto your toes slowly, then lower with control.",
    "Marching in place": "March at a comfortable pace, keep your breathing steady, and use a smaller step if needed.",
    "Bodyweight squats": "Keep your feet comfortably apart and lower your hips only as far as you can with control.",
    "Incline push-ups": "Use a stable wall or counter. Keep your body aligned and choose a height that feels manageable.",
    "Reverse lunges": "Hold a stable surface if needed. Step one foot back a short distance and bend only as far as comfortable.",
    "Push-ups": "Keep your body aligned and use a wall or stable incline to make the movement easier.",
    "Plank": "Keep your body in a comfortable straight line and breathe normally. Use a wall or counter version if needed.",
}
@app.get("/api/workout")
def workout(minutes: int = 30, x_user_id: str = Header(None)):
    """Rules-based plan. No AI involved, so it is predictable and safe."""
    uid = uid_ok(x_user_id)
    with db() as c:
        p = c.execute("SELECT level, equipment, age_group FROM profiles WHERE uid=?", (uid,)).fetchone()
    adult = bool(p and p["age_group"] == "adult")
    level = p["level"] if p and p["level"] in PLANS else "beginner"
    if not adult and level == "advanced": level = "intermediate"
    # Keep duration bounded and the plan approachable. Exercise counts are a
    # rough guide; actual duration varies with pace, breaks, and experience.
    minutes = max(5, min(45, minutes))
    n = max(2, min(5, (minutes + 4) // 5))
    # Longer choices increase the set count as well as the number of exercises.
    # This remains an estimate; pace and breaks determine the real duration.
    set_count = max(1, min(4, (minutes + 9) // 10))
    plan = list(PLANS[level][:n])
    plan = [(name, re.sub(r"^\d+", str(set_count), sets)) for name, sets in plan]
    equipment = (p["equipment"] or "").lower() if p else ""
    if adult and "dumbbell" in equipment and level != "beginner" and plan:
        plan[-1] = ("Dumbbell goblet squat", "2 x 10")
        EXERCISE_GUIDES["Dumbbell goblet squat"] = "Hold one dumbbell close to your chest and squat only as far as comfortable. Choose a light load and prioritize control."
    note = (f"Designed as a roughly {minutes}-minute beginner-friendly session; your pace and rest breaks may change the total time. "
             "Warm up gently, move at a comfortable pace, and stop if you feel pain, dizziness, or unwell. "
             "This is general guidance, not medical advice.")
    if not adult: note += " Under 18: focus on good form, rest days, and ask a teacher or coach before heavy lifting."
    return {"level": level, "minutes": minutes, "exercises": [
        {"name": name, "sets": sets, "instructions": EXERCISE_GUIDES.get(name, "Move slowly, keep a comfortable range of motion, and prioritize good form.")}
        for name, sets in plan
    ], "note": note}

@app.post("/api/workout/done")
def workout_done(x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c: c.execute("INSERT INTO workouts(uid,day) VALUES(?,?)", (uid, today()))
    return {"ok": True}

COACH_RULES = ("You are FitAI, a friendly general fitness and healthy-eating coach. Answer in under 150 words. "
               "Do not diagnose conditions, do not prescribe calorie targets or weight-loss diets, and never encourage "
               "extreme restriction or training through pain. For medical issues, injuries, or if the user may be under 18 and "
               "asks about losing weight, tell them to talk to a doctor or qualified professional. Use only the data provided.")

TEEN_RULES = (" The user is under 18 or their age is unknown: never discuss losing weight, calorie targets, or restrictive eating. "
              "Focus on balanced meals, sleep, play, and general activity, and suggest talking to a parent, doctor, or school coach about body-weight concerns.")

@app.post("/api/coach")
def coach(a: Ask, x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    spend_ai(uid)
    d = dashboard(x_user_id)
    with db() as c:
        p = c.execute("SELECT goal, level, equipment, diet, age_group FROM profiles WHERE uid=?", (uid,)).fetchone()
    rules = COACH_RULES if (p and p["age_group"] == "adult") else COACH_RULES + TEEN_RULES
    ctx = f"Profile: {dict(p) if p else 'none'}. Today's totals: {d['totals']}. Workouts this week: {d['workouts_this_week']}."
    return {"answer": gemini(f"{ctx}\nQuestion: {a.question[:500]}", rules)}

@app.get("/api/usage")
def usage(x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c:
        row = c.execute("SELECT n FROM ai_usage WHERE uid=? AND day=?", (uid, today())).fetchone()
    return {"used": row["n"] if row else 0, "limit": USER_LIMIT, "retention_days": RETENTION_DAYS}

@app.get("/api/export")
def export(x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c:
        return {t: [dict(r) for r in c.execute(f"SELECT * FROM {t} WHERE uid=?", (uid,))]
                for t in ("profiles", "food", "workouts")}

@app.delete("/api/data")
def delete_data(x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c:
        for t in ("profiles", "food", "workouts", "ai_usage"):
            c.execute(f"DELETE FROM {t} WHERE uid=?", (uid,))
    return {"deleted": True}

@app.get("/")
def home(): return FileResponse("static/index.html")
app.mount("/static", StaticFiles(directory="static"), name="static")
