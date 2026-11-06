"""FitAI backend: FastAPI + SQLite + Gemini. Run: uvicorn main:app --reload"""
import os, json, re, sqlite3, datetime as dt, math
from io import BytesIO
from xml.sax.saxutils import escape
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from typing import Literal
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether,
)

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
        migrations = (
            "ALTER TABLE profiles ADD COLUMN age_group TEXT",
            "ALTER TABLE food ADD COLUMN meal_type TEXT NOT NULL DEFAULT 'Snack'",
            "ALTER TABLE food ADD COLUMN portion TEXT NOT NULL DEFAULT 'Standard serving'",
        )
        for statement in migrations:
            try:
                c.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

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
            r"\s*(\d+(?:\.\d+)?)\s*(?:(kg|g|grams?|oz|ounces?|lbs?|pounds?)(?=\s|$)\s*)?(?:of\s+)?(.+?)\s*$",
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
    portion_id: str = "standard"
    servings: float = 1
    meal_type: Literal["Breakfast", "Lunch", "Dinner", "Snack"] = "Snack"

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

def food_portions(food):
    """Offer practical household portions; all weights remain visible as estimates."""
    grams = float(food["serving_g"])
    label = food["serving_label"]
    portions = [
        {"id": "half", "label": f"Half {label} (~{round(grams / 2)} g)", "grams": round(grams / 2, 1)},
        {"id": "standard", "label": f"{label} (~{round(grams)} g)", "grams": grams},
        {"id": "double", "label": f"2 x {label} (~{round(grams * 2)} g)", "grams": round(grams * 2, 1)},
    ]
    if food["category"] in {"Grains", "Protein", "Vegetables", "Legumes"}:
        portions.append({"id": "plate", "label": "1 plate portion (~300 g guide)", "grams": 300})
    if food["category"] in {"Grains", "Legumes", "Vegetables"}:
        portions.append({"id": "bowl", "label": "1 bowl portion (~200 g guide)", "grams": 200})
    return portions


@app.get("/api/foods")
def list_foods():
    """Return searchable foods with standard portion choices."""
    return {
        "source_note": "Generic reference estimates. Household portions and plate sizes are guides, not exact measurements.",
        "foods": [
            {
                **{key: value for key, value in food.items() if key != "aliases"},
                "quick": food["id"] in QUICK_FOODS,
                "portions": food_portions(food),
            }
            for food in FOOD_CATALOG
        ],
    }


def save_food_entry(uid, food_id, grams, meal_type="Snack", portion_label="Standard serving"):
    food = FOOD_BY_ID[food_id]
    ratio = grams / 100.0
    kcal = food["kcal_per_100g"] * ratio
    protein = food["protein_per_100g"] * ratio
    carbs = food["carbs_per_100g"] * ratio
    fat = food["fat_per_100g"] * ratio
    with db() as c:
        c.execute(
            """INSERT INTO food(
                uid,day,name,grams,kcal,protein,carbs,fat,meal_type,portion
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (uid, today(), food["name"], grams, kcal, protein, carbs, fat, meal_type, portion_label),
        )
    return {
        "id": food_id, "name": food["name"], "grams": round(grams, 1),
        "kcal": round(kcal, 1), "protein": round(protein, 1),
        "carbs": round(carbs, 1), "fat": round(fat, 1),
        "meal_type": meal_type, "portion": portion_label,
    }


@app.post("/api/food/item")
def log_food_item(entry: FoodEntry, x_user_id: str = Header(None)):
    """Log a selected food by familiar portion and number of servings."""
    uid = uid_ok(x_user_id)
    food = FOOD_BY_ID.get(entry.food_id)
    if not food:
        raise HTTPException(404, "That food is not in the catalogue.")
    if not math.isfinite(entry.servings) or not 0 < entry.servings <= 10:
        raise HTTPException(422, "Choose between 0 and 10 servings.")
    portion = next((item for item in food_portions(food) if item["id"] == entry.portion_id), None)
    if not portion:
        raise HTTPException(422, "That portion is not available for this food.")
    grams = portion["grams"] * entry.servings
    if not math.isfinite(grams) or not 0 < grams <= 3000:
        raise HTTPException(422, "The selected portion is outside the supported range.")
    if entry.servings == 1:
        portion_label = portion["label"]
    else:
        portion_label = f"{entry.servings:g} x {portion['label']}"
    logged = save_food_entry(uid, entry.food_id, grams, entry.meal_type, portion_label)
    return {"logged": [logged], "not_in_database": []}


@app.post("/api/food")
def log_food(m: Meal, x_user_id: str = Header(None)):
    """Backward-compatible text logging endpoint; catalogue matches remain exact."""
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
            + ". Do not invent a catalogue item or calculate nutrition. JSON only."
        )
        raw = gemini(
            prompt,
            "Extract food names and gram amounts only. Do not calculate calories or macros. "
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
        food = FOOD_BY_ID[food_id]
        portion_label = f"Text entry (~{round(grams)} g)"
        logged.append(save_food_entry(uid, food_id, grams, "Snack", portion_label))
    return {"logged": logged, "not_in_database": unknown}

@app.get("/api/dashboard")
def dashboard(x_user_id: str = Header(None)):
    uid = uid_ok(x_user_id)
    with db() as c:
        meals = [dict(r) for r in c.execute(
            "SELECT name,grams,kcal,protein,carbs,fat,meal_type,portion FROM food WHERE uid=? AND day=? ORDER BY id",
            (uid, today()))]
        week = [dict(r) for r in c.execute(
            "SELECT day, ROUND(SUM(kcal)) kcal FROM food WHERE uid=? AND day>=? GROUP BY day ORDER BY day",
            (uid, (dt.date.today() - dt.timedelta(days=6)).isoformat()))]
        wk = c.execute("SELECT COUNT(*) FROM workouts WHERE uid=? AND day>=?",
                       (uid, (dt.date.today() - dt.timedelta(days=6)).isoformat())).fetchone()[0]
    s = lambda k: round(sum(m[k] for m in meals))
    return {"meals": meals, "totals": {"kcal": s("kcal"), "protein": s("protein"), "carbs": s("carbs"), "fat": s("fat")},
            "week": week, "workouts_this_week": wk}

WORKOUT_LIBRARY = {
    "beginner": [
        ("Comfortable march in place", "March gently at a pace where you can speak comfortably."),
        ("Chair sit-to-stands", "Use a stable chair. Stand and sit with control, or make the movement smaller."),
        ("Wall push-ups", "Keep your body aligned and use a comfortable range of motion."),
        ("Standing calf raises", "Hold a stable support if needed and rise gently onto your toes."),
        ("Bird-dog reach", "From hands and knees, extend opposite arm and leg only as far as comfortable."),
    ],
    "intermediate": [
        ("March with relaxed arm swings", "Keep a steady, comfortable pace and let your arms move naturally."),
        ("Bodyweight squats", "Use a comfortable depth and keep each repetition controlled."),
        ("Incline push-ups", "Use a stable wall or counter and choose a comfortable angle."),
        ("Alternating step-backs", "Step one foot back at a time; use support and a shallow range if needed."),
        ("Bird-dog reach", "Move slowly from hands and knees, keeping your balance and breathing normally."),
    ],
    "advanced": [
        ("March with alternating knee lifts", "Keep the pace controlled and lift each knee only as high as feels comfortable."),
        ("Bodyweight squats with a pause", "Pause briefly at a comfortable depth; keep each movement smooth."),
        ("Incline push-ups", "Choose a stable surface and maintain a controlled range of motion."),
        ("Alternating reverse lunges", "Use a comfortable step and hold support if balance is uncertain."),
        ("Bird-dog reach", "Move slowly and focus on steady balance and breathing."),
    ],
}
WARMUP_ITEMS = [
    ("Easy walk or march", "Begin gently and gradually become more comfortable with the movement."),
    ("Shoulder rolls", "Make small, relaxed circles without forcing the range."),
    ("Side steps", "Step gently from side to side, keeping the movement controlled."),
]
COOLDOWN_ITEMS = [
    ("Slow walk and easy breathing", "Gradually slow down and let your breathing return to normal."),
    ("Gentle shoulder and calf stretches", "Stretch only to a comfortable sensation; do not bounce or force the stretch."),
]


def split_durations(total_seconds, count):
    base, remainder = divmod(total_seconds, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


@app.get("/api/workout")
def workout(minutes: int = 30, x_user_id: str = Header(None)):
    """Build a timed, moderate session with warm-up, circuit recovery and cool-down."""
    uid = uid_ok(x_user_id)
    with db() as c:
        profile = c.execute(
            "SELECT level, age_group FROM profiles WHERE uid=?", (uid,)
        ).fetchone()

    age_group = profile["age_group"] if profile else None
    level = profile["level"] if profile and profile["level"] in WORKOUT_LIBRARY else "beginner"
    if age_group != "adult" and level == "advanced":
        level = "intermediate"

    minutes = max(5, min(45, int(minutes)))
    total_seconds = minutes * 60
    warmup_seconds = 60 if minutes == 5 else int(round(total_seconds * 0.15 / 15) * 15)
    cooldown_seconds = warmup_seconds
    circuit_seconds = total_seconds - warmup_seconds - cooldown_seconds

    library = WORKOUT_LIBRARY[level]
    exercise_count = 4 if minutes == 5 else len(library)
    rounds = 1 if minutes == 5 else max(2, min(6, round(circuit_seconds / (exercise_count * 60))))
    slot_seconds = circuit_seconds // (exercise_count * rounds)
    active_seconds = max(20, min(45, round(slot_seconds * 0.70)))
    recovery_seconds = max(0, slot_seconds - active_seconds)

    exercises = []
    warmup_count = 2 if warmup_seconds < 90 else 3
    warmup_durations = split_durations(warmup_seconds, warmup_count)
    for index, (name, instructions) in enumerate(WARMUP_ITEMS[:warmup_count]):
        duration = warmup_durations[index]
        exercises.append({
            "name": name, "block": "Warm-up",
            "sets": f"{duration} seconds",
            "duration_seconds": duration, "rest_seconds": 0, "rounds": 1,
            "total_seconds": duration, "instructions": instructions,
        })

    for name, instructions in library[:exercise_count]:
        exercises.append({
            "name": name, "block": "Main circuit",
            "sets": f"{rounds} rounds - {active_seconds} sec movement + {recovery_seconds} sec recovery",
            "duration_seconds": active_seconds, "rest_seconds": recovery_seconds,
            "rounds": rounds, "total_seconds": slot_seconds * rounds,
            "instructions": instructions,
        })

    cooldown_durations = split_durations(cooldown_seconds, len(COOLDOWN_ITEMS))
    for index, (name, instructions) in enumerate(COOLDOWN_ITEMS):
        duration = cooldown_durations[index]
        exercises.append({
            "name": name, "block": "Cool-down",
            "sets": f"{duration} seconds",
            "duration_seconds": duration, "rest_seconds": 0, "rounds": 1,
            "total_seconds": duration, "instructions": instructions,
        })

    planned_seconds = sum(item["total_seconds"] for item in exercises)
    note = (
        f"This {minutes}-minute plan includes a warm-up, a timed movement circuit, built-in recovery "
        "between movements, and a cool-down. Move at a manageable pace; the intervals are guides, "
        "not targets to beat. Pause, adjust or skip any movement as needed. Stop if you feel pain, "
        "dizziness or unwell. This is general activity guidance, not medical advice."
    )
    if age_group != "adult":
        note += " Under 18: prioritise enjoyable movement, technique and recovery; ask a qualified coach or trusted adult if unsure."
    return {
        "level": level, "minutes": minutes,
        "total_seconds": total_seconds, "planned_seconds": planned_seconds,
        "warmup_seconds": warmup_seconds, "circuit_seconds": circuit_seconds,
        "cooldown_seconds": cooldown_seconds, "rounds": rounds,
        "exercises": exercises, "note": note,
    }

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


@app.get("/api/export/pdf")
def export_pdf(x_user_id: str = Header(None)):
    """Create a readable, paginated PDF containing this browser profile's data."""
    uid = uid_ok(x_user_id)
    with db() as c:
        profile = c.execute(
            "SELECT goal,level,equipment,diet,age_group FROM profiles WHERE uid=?", (uid,)
        ).fetchone()
        foods = [dict(row) for row in c.execute(
            """SELECT day,meal_type,name,portion,grams,kcal,protein,carbs,fat
               FROM food WHERE uid=? ORDER BY day DESC,id ASC""", (uid,)
        )]
        workouts = [dict(row) for row in c.execute(
            "SELECT day,COUNT(*) AS sessions FROM workouts WHERE uid=? GROUP BY day ORDER BY day DESC",
            (uid,),
        )]

    def clean(value):
        value = str(value or "")
        value = (value.replace("½", "1/2").replace("–", "-").replace("—", "-")
                      .replace("×", "x").replace("·", "-").replace("’", "'"))
        return escape(value)

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm,
        topMargin=17 * mm, bottomMargin=18 * mm,
        title="FitAI Personal Activity Report",
        author="FitAI",
    )
    navy = colors.HexColor("#10243A")
    blue = colors.HexColor("#2B59FF")
    pale = colors.HexColor("#EEF3FA")
    line = colors.HexColor("#D9E2EF")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="FitTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=22, leading=27, textColor=navy, alignment=TA_LEFT, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        name="FitSub", parent=styles["Normal"], fontSize=9, leading=13,
        textColor=colors.HexColor("#5A6B7F"), spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        name="FitSection", parent=styles["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=15, textColor=navy, spaceBefore=10, spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="FitBody", parent=styles["BodyText"], fontSize=8.5, leading=12,
        textColor=colors.HexColor("#26384B"),
    ))
    story = [
        Paragraph("FitAI", styles["FitTitle"]),
        Paragraph("Personal nutrition and activity report", styles["Heading2"]),
        Paragraph(
            f"Generated {clean(dt.datetime.now().strftime('%d %B %Y, %H:%M'))} - "
            "A private export for the browser profile requesting this report.",
            styles["FitSub"],
        ),
    ]

    summary = [
        ["FOOD ENTRIES", "WORKOUT DAYS", "PROFILE"],
        [str(len(foods)), str(sum(int(item["sessions"]) for item in workouts)),
         "Set up" if profile else "Not set up"],
    ]
    summary_table = Table(summary, colWidths=[55 * mm, 55 * mm, 55 * mm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), navy),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("BACKGROUND", (0, 1), (-1, 1), pale),
        ("TEXTCOLOR", (0, 1), (-1, 1), navy),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 15),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 0.5, line),
    ]))
    story.extend([summary_table, Spacer(1, 5 * mm)])

    story.append(Paragraph("Profile", styles["FitSection"]))
    if profile:
        profile_rows = [
            ["Age group", clean(profile["age_group"] or "Not specified")],
            ["Goal", clean(profile["goal"] or "Not specified")],
            ["Experience", clean(profile["level"] or "Not specified")],
            ["Equipment", clean(profile["equipment"] or "Not specified")],
            ["Food preferences", clean(profile["diet"] or "Not specified")],
        ]
        profile_table = Table(profile_rows, colWidths=[42 * mm, 123 * mm])
        profile_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), pale),
            ("TEXTCOLOR", (0, 0), (-1, -1), navy),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.35, line),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(profile_table)
    else:
        story.append(Paragraph("No profile has been saved in this browser profile.", styles["FitBody"]))

    story.append(Paragraph("Food diary", styles["FitSection"]))
    if foods:
        table_rows = [["Date", "Meal", "Food", "Portion", "Energy*"]]
        for item in foods:
            table_rows.append([
                clean(item["day"]), clean(item["meal_type"] or "Snack"),
                Paragraph(clean(item["name"]), styles["FitBody"]),
                Paragraph(clean(item["portion"] or "Standard serving"), styles["FitBody"]),
                f"{round(float(item['kcal']))} kcal",
            ])
        food_table = Table(table_rows, colWidths=[24 * mm, 25 * mm, 55 * mm, 39 * mm, 22 * mm], repeatRows=1)
        food_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), blue),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.35, line),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, pale]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(food_table)
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(
            "*Calories are approximate reference estimates. Actual values vary by ingredient, brand, recipe, preparation and serving size.",
            styles["FitSub"],
        ))
    else:
        story.append(Paragraph("No food entries have been recorded yet.", styles["FitBody"]))

    story.append(Paragraph("Workout history", styles["FitSection"]))
    if workouts:
        workout_rows = [["Date", "Sessions recorded"]]
        workout_rows.extend([[clean(row["day"]), str(row["sessions"])] for row in workouts])
        workout_table = Table(workout_rows, colWidths=[55 * mm, 40 * mm], repeatRows=1)
        workout_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), navy),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.35, line),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, pale]),
            ("ALIGN", (1, 0), (1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(workout_table)
    else:
        story.append(Paragraph("No completed workouts have been recorded yet.", styles["FitBody"]))

    story.extend([
        Spacer(1, 5 * mm),
        Paragraph(
            "FitAI provides general wellness information, not medical advice. This report does not set calorie targets "
            "or recommend restrictive eating. Keep the report private when it contains personal information.",
            styles["FitSub"],
        ),
    ])

    def draw_page(canvas, doc):
        canvas.saveState()
        page_width, _ = A4
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.5)
        canvas.line(16 * mm, 13 * mm, page_width - 16 * mm, 13 * mm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#68788B"))
        canvas.drawString(16 * mm, 8 * mm, "FitAI - Personal data export")
        canvas.drawRightString(page_width - 16 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    pdf = buffer.getvalue()
    filename = f"fitai-report-{today()}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

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
