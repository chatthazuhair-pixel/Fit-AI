"""FitAI backend: FastAPI + SQLite + Gemini. Run: uvicorn main:app --reload"""
import os, json, re, sqlite3, datetime as dt
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal
from pydantic import BaseModel

DB = os.getenv("DB_PATH", "fitai.db")
KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
USER_LIMIT = int(os.getenv("USER_DAILY_LIMIT", "10"))
GLOBAL_LIMIT = int(os.getenv("GLOBAL_DAILY_LIMIT", "200"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

app = FastAPI(title="FitAI")

# STARTER DATA per 100 g: (kcal, protein, carbs, fat). Approximate values.
# TODO: verify every row against USDA FoodData Central and add local dishes.
NUTR = {
    "roti": (297, 9, 50, 7), "rice": (130, 2.7, 28, 0.3),
    "chicken breast": (165, 31, 0, 3.6), "egg": (143, 12.6, 0.7, 9.5),
    "daal": (116, 9, 20, 0.4), "banana": (89, 1.1, 23, 0.3),
    "milk": (61, 3.2, 4.8, 3.3), "apple": (52, 0.3, 14, 0.2),
    "bread": (265, 9, 49, 3.2), "potato": (87, 1.9, 20, 0.1),
    "yogurt": (61, 3.5, 4.7, 3.3), "oats": (389, 17, 66, 7),
    # Additional approximate values per 100 g; recipes and brands vary.
    "chickpeas": (164, 8.9, 27.4, 2.6), "lentils": (116, 9, 20, 0.4),
    "fish": (128, 26, 0, 2.7), "beef": (250, 26, 0, 15),
    "cucumber": (15, 0.7, 3.6, 0.1), "tomato": (18, 0.9, 3.9, 0.2),
    "spinach": (23, 2.9, 3.6, 0.4), "mango": (60, 0.8, 15, 0.4),
    "dates": (282, 2.5, 75, 0.4), "peanut butter": (588, 25, 20, 50),
    "paneer": (265, 18, 1.2, 20), "beans": (127, 8.7, 22.8, 0.5),
}
PIECE_G = {"roti": 40, "egg": 50, "banana": 118, "apple": 182, "bread": 30, "potato": 150}

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
    """No-AI parser: handles '2 roti, 150g rice, 1 egg'."""
    items = []
    for part in text.lower().split(","):
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(g|gram|grams)?\s*(?:of\s+)?(.+)", part)
        if not m: continue
        qty, unit, name = float(m.group(1)), m.group(2), m.group(3).strip()
        key = next((k for k in NUTR if k in name), None)
        if key:
            items.append({"name": key, "grams": qty if unit else qty * PIECE_G.get(key, 100)})
    return items

class Profile(BaseModel):
    age_group: Literal["teen", "adult"]; goal: str; level: str; equipment: str; diet: str = ""
class Meal(BaseModel):
    text: str
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

@app.post("/api/food")
def log_food(m: Meal, x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    text = m.text[:300]
    items = []
    if KEY:
        spend_ai(uid)
        raw = gemini(f"Meal: {text}", "Extract foods as JSON list of {name, grams}. Use these names when possible: "
                     + ", ".join(NUTR) + ". Estimate grams from typical portions. JSON only.", json_mode=True)
        try: items = json.loads(raw)
        except Exception: items = []
    if not items: items = fallback_parse(text)
    logged, unknown = [], []
    with db() as c:
        for it in items:
            name = str(it.get("name", "")).lower()
            key = next((k for k in NUTR if k in name), None)
            try: g = max(0, min(float(it.get("grams", 0)), 2000))
            except Exception: continue
            if not key: unknown.append(name); continue
            k, p, cb, f = (v * g / 100 for v in NUTR[key])
            c.execute("INSERT INTO food(uid,day,name,grams,kcal,protein,carbs,fat) VALUES(?,?,?,?,?,?,?,?)",
                      (uid, today(), key, g, k, p, cb, f))
            logged.append({"name": key, "grams": round(g), "kcal": round(k)})
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
    n = max(2, min(5, minutes // 5))
    plan = list(PLANS[level][:n])
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
