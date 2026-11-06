import os
from pathlib import Path
import sys

# Ensure the repository root is importable when pytest collects this file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Give tests an isolated SQLite file before importing the app.
os.environ["DB_PATH"] = str(Path("/tmp") / "fitai-test-suite.db")
os.environ.setdefault("GEMINI_API_KEY", "")

from fastapi.testclient import TestClient
import main


client = TestClient(main.app)
USER_ID = "fitai-test-user-01"
HEADERS = {"X-User-Id": USER_ID}


def clear_test_data():
    with main.db() as conn:
        for table in ("profiles", "food", "ai_usage", "workouts"):
            conn.execute(f"DELETE FROM {table}")


def setup_function():
    clear_test_data()


def test_homepage_serves_fitai():
    response = client.get("/")
    assert response.status_code == 200
    assert "FitAI" in response.text
    assert "guideCard" in response.text


def test_profile_round_trip_and_workout_personalization():
    payload = {
        "age_group": "teen",
        "goal": "general_fitness",
        "level": "advanced",
        "equipment": "none",
        "diet": "",
    }
    saved = client.post("/api/profile", headers=HEADERS, json=payload)
    assert saved.status_code == 200
    profile = client.get("/api/profile", headers=HEADERS)
    assert profile.status_code == 200
    assert profile.json()["age_group"] == "teen"

    workout = client.get("/api/workout?minutes=15", headers=HEADERS)
    assert workout.status_code == 200
    data = workout.json()
    assert data["level"] == "intermediate"  # advanced is capped for teens
    assert data["minutes"] == 15
    assert len(data["exercises"]) == 3
    assert all(item.get("instructions") for item in data["exercises"])
    assert all(item["sets"].startswith("2 x ") for item in data["exercises"])
    assert "Under 18" in data["note"]


def test_workout_duration_is_bounded_and_scales_sets():
    short = client.get("/api/workout?minutes=1", headers=HEADERS).json()
    long = client.get("/api/workout?minutes=999", headers=HEADERS).json()
    assert short["minutes"] == 5
    assert len(short["exercises"]) == 2
    assert all(item["sets"].startswith("1 x ") for item in short["exercises"])
    assert long["minutes"] == 45
    assert len(long["exercises"]) == 5
    assert all(item["sets"].startswith("4 x ") for item in long["exercises"])


def test_food_fallback_logs_known_foods_without_ai():
    main.KEY = ""
    response = client.post(
        "/api/food", headers=HEADERS,
        json={"text": "2 eggs, 150g cooked white rice"},
    )
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["logged"]} == {
        "egg_boiled", "white_rice_cooked",
    }
    entries = {item["id"]: item for item in response.json()["logged"]}
    assert entries["egg_boiled"]["grams"] == 100
    assert entries["white_rice_cooked"]["grams"] == 150


def test_food_catalogue_has_many_global_foods_and_no_local_entries():
    response = client.get("/api/foods")
    assert response.status_code == 200
    foods = response.json()["foods"]
    assert len(foods) >= 60
    names = [food["name"].casefold() for food in foods]
    assert any("salmon" in name for name in names)
    assert any("quinoa" in name for name in names)
    assert any("blueberries" in name for name in names)
    assert all("roti" not in name and "daal" not in name for name in names)


def test_selected_food_calories_use_exact_food_and_gram_weight():
    response = client.post(
        "/api/food/item", headers=HEADERS,
        json={"food_id": "chicken_breast_cooked", "grams": 150},
    )
    assert response.status_code == 200
    logged = response.json()["logged"][0]
    assert logged["name"] == main.FOOD_BY_ID["chicken_breast_cooked"]["name"]
    assert logged["grams"] == 150
    assert logged["kcal"] == 247.5


def test_selected_food_rejects_invalid_weight_and_unknown_id():
    invalid_weight = client.post(
        "/api/food/item", headers=HEADERS,
        json={"food_id": "banana_raw", "grams": -5},
    )
    unknown_food = client.post(
        "/api/food/item", headers=HEADERS,
        json={"food_id": "not_a_food", "grams": 100},
    )
    assert invalid_weight.status_code == 422
    assert unknown_food.status_code == 404


def test_food_parser_does_not_guess_by_partial_substrings():
    main.KEY = ""
    response = client.post(
        "/api/food", headers=HEADERS,
        json={"text": "1 chicken sandwich"},
    )
    assert response.status_code == 200
    assert response.json()["logged"] == []


def test_data_delete_clears_profile_food_and_workouts():
    client.post("/api/profile", headers=HEADERS, json={
        "age_group": "teen", "goal": "general_fitness",
        "level": "beginner", "equipment": "none", "diet": "",
    })
    client.post("/api/food", headers=HEADERS, json={"text": "1 egg"})
    client.post("/api/workout/done", headers=HEADERS)
    response = client.delete("/api/data", headers=HEADERS)
    assert response.status_code == 200
    assert client.get("/api/profile", headers=HEADERS).json() == {}
    dashboard = client.get("/api/dashboard", headers=HEADERS).json()
    assert dashboard["meals"] == []
    assert dashboard["workouts_this_week"] == 0


def test_invalid_user_id_is_rejected():
    response = client.get("/api/dashboard", headers={"X-User-Id": "bad"})
    assert response.status_code == 400
