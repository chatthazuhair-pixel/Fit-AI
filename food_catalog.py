"""Searchable generic food catalogue for FitAI.

Values are approximate generic reference values per 100 g for the preparation
named in each label. They are informed by USDA FoodData Central-style records,
not live product labels or a live database feed.
"""
import re

# id, display name, category, kcal, protein_g, carbs_g, fat_g,
# typical serving grams, serving description, aliases
_ROWS = [
("white_rice_cooked","White rice, cooked","Grains",130,2.69,28.17,0.28,158,"1 cup",["rice","white rice","cooked rice","cooked white rice"]),
("brown_rice_cooked","Brown rice, cooked","Grains",123,2.74,25.58,0.97,195,"1 cup",["brown rice","cooked brown rice"]),
("pasta_cooked","Pasta, cooked","Grains",157,5.80,30.86,0.93,140,"1 cup",["pasta","cooked pasta","spaghetti","macaroni"]),
("oats_dry","Rolled oats, dry","Grains",389,16.89,66.27,6.90,40,"½ cup",["oats","rolled oats","dry oats"]),
("quinoa_cooked","Quinoa, cooked","Grains",120,4.40,21.30,1.92,185,"1 cup",["quinoa","cooked quinoa"]),
("whole_wheat_bread","Whole-wheat bread","Grains",252,12.45,43.10,3.50,32,"1 slice",["whole wheat bread","brown bread"]),
("white_bread","White bread","Grains",266,8.85,49.42,3.33,30,"1 slice",["white bread","sandwich bread"]),
("potato_baked","Potato, baked with skin","Grains",93,2.50,21.15,0.13,173,"1 medium potato",["baked potato","potato"]),
("sweet_potato_baked","Sweet potato, baked","Grains",90,2.01,20.71,0.15,130,"1 medium sweet potato",["sweet potato","baked sweet potato"]),
("corn_cooked","Sweet corn, cooked","Grains",96,3.41,20.98,1.50,82,"½ cup",["corn","sweet corn","cooked corn"]),
("couscous_cooked","Couscous, cooked","Grains",112,3.79,23.22,0.16,157,"1 cup",["couscous","cooked couscous"]),
("corn_tortilla","Corn tortilla","Grains",218,5.70,44.60,2.85,28,"1 small tortilla",["tortilla","corn tortillas"]),

("chicken_breast_cooked","Chicken breast, cooked, skinless","Protein",165,31.02,0.00,3.57,120,"1 small portion",["chicken breast","cooked chicken breast","chicken"]),
("chicken_thigh_cooked","Chicken thigh, cooked, meat only","Protein",209,26.00,0.00,10.90,100,"1 portion",["chicken thigh","cooked chicken thigh"]),
("turkey_breast_cooked","Turkey breast, roasted","Protein",135,29.00,0.00,1.70,85,"3 oz portion",["turkey","turkey breast","roasted turkey"]),
("beef_steak_cooked","Beef steak, lean, cooked","Protein",217,26.00,0.00,12.00,100,"1 portion",["beef steak","steak","beef"]),
("ground_beef_cooked","Ground beef, 90% lean, cooked","Protein",230,28.50,0.00,12.00,100,"1 portion",["ground beef","minced beef"]),
("pork_tenderloin_cooked","Pork tenderloin, cooked","Protein",143,26.00,0.00,3.50,100,"1 portion",["pork","pork tenderloin"]),
("salmon_cooked","Salmon, cooked","Protein",206,22.10,0.00,12.40,120,"1 fillet portion",["salmon","cooked salmon"]),
("cod_cooked","Cod, cooked","Protein",89,19.90,0.00,0.73,120,"1 fillet portion",["cod","cooked cod","white fish cooked"]),
("tilapia_cooked","Tilapia, cooked","Protein",128,26.15,0.00,2.65,120,"1 fillet portion",["tilapia","cooked tilapia"]),
("haddock_cooked","Haddock, cooked","Protein",90,20.60,0.00,0.55,120,"1 fillet portion",["haddock","cooked haddock"]),
("trout_cooked","Rainbow trout, cooked","Protein",168,23.80,0.00,7.40,120,"1 fillet portion",["trout","rainbow trout","cooked trout"]),
("mackerel_cooked","Atlantic mackerel, cooked","Protein",262,23.85,0.00,17.81,100,"1 portion",["mackerel","atlantic mackerel","cooked mackerel"]),
("sardines_canned","Sardines, canned in oil, drained","Protein",208,24.62,0.00,11.45,85,"1 small tin drained",["sardines","canned sardines"]),
("pollock_cooked","Pollock, cooked","Protein",111,23.48,0.00,1.18,120,"1 fillet portion",["pollock","cooked pollock"]),
("catfish_cooked","Catfish, cooked","Protein",144,18.44,0.00,7.19,120,"1 fillet portion",["catfish","cooked catfish"]),
("halibut_cooked","Halibut, cooked","Protein",140,26.00,0.00,3.00,120,"1 fillet portion",["halibut","cooked halibut"]),
("sardines_fresh_cooked","Sardines, fresh, cooked","Protein",208,24.60,0.00,11.50,100,"1 portion",["fresh sardines","grilled sardines"]),
("tuna_canned","Tuna, canned in water, drained","Protein",116,25.50,0.00,0.82,85,"1 small can drained",["tuna","canned tuna","tuna in water"]),
("shrimp_cooked","Shrimp, cooked","Protein",99,24.00,0.20,0.28,85,"3 oz portion",["shrimp","prawns","cooked shrimp"]),
("egg_boiled","Egg, whole, hard-boiled","Protein",155,12.58,1.12,10.61,50,"1 large egg",["egg","eggs","boiled egg","hard boiled egg","hard-boiled egg"]),
("tofu_firm","Tofu, firm","Protein",144,17.30,2.78,8.72,85,"3 oz portion",["tofu","firm tofu"]),
("tempeh","Tempeh","Protein",192,20.29,7.64,10.80,85,"3 oz portion",["tempeh"]),

("milk_whole","Milk, whole","Dairy & alternatives",61,3.15,4.80,3.25,244,"1 cup",["whole milk","full-fat milk","milk"]),
("milk_skim","Milk, skim","Dairy & alternatives",34,3.37,4.96,0.08,245,"1 cup",["skim milk","skimmed milk","fat-free milk"]),
("greek_yogurt_plain","Greek yogurt, plain, nonfat","Dairy & alternatives",59,10.19,3.60,0.39,170,"1 small tub",["greek yogurt","plain greek yogurt"]),
("yogurt_plain","Yogurt, plain, whole milk","Dairy & alternatives",61,3.47,4.66,3.25,170,"1 small tub",["plain yogurt","yogurt"]),
("cheddar_cheese","Cheddar cheese","Dairy & alternatives",403,24.90,1.28,33.14,28,"1 oz",["cheddar","cheddar cheese"]),
("mozzarella_cheese","Mozzarella cheese, whole milk","Dairy & alternatives",300,22.17,2.19,22.35,28,"1 oz",["mozzarella","mozzarella cheese"]),
("cottage_cheese","Cottage cheese, low-fat","Dairy & alternatives",82,11.12,4.31,2.30,113,"½ cup",["cottage cheese"]),
("almond_milk_unsweetened","Almond milk, unsweetened","Dairy & alternatives",15,0.55,0.60,1.10,244,"1 cup",["almond milk","unsweetened almond milk"]),
("soy_milk_unsweetened","Soy milk, unsweetened","Dairy & alternatives",33,2.90,1.67,1.61,243,"1 cup",["soy milk","unsweetened soy milk"]),

("apple_raw","Apple, raw, with skin","Fruit",52,0.26,13.81,0.17,182,"1 medium apple",["apple","apples"]),
("banana_raw","Banana, raw","Fruit",89,1.09,22.84,0.33,118,"1 medium banana",["banana","bananas"]),
("orange_raw","Orange, raw","Fruit",47,0.94,11.75,0.12,131,"1 medium orange",["orange","oranges"]),
("blueberries_raw","Blueberries, raw","Fruit",57,0.74,14.49,0.33,148,"1 cup",["blueberries","blueberry"]),
("strawberries_raw","Strawberries, raw","Fruit",32,0.67,7.68,0.30,152,"1 cup",["strawberries","strawberry"]),
("grapes_raw","Grapes, raw","Fruit",69,0.72,18.10,0.16,92,"½ cup",["grapes","grape"]),
("mango_raw","Mango, raw","Fruit",60,0.82,14.98,0.38,165,"1 cup pieces",["mango","mangoes"]),
("avocado_raw","Avocado, raw","Fruit",160,2.00,8.53,14.66,150,"½ medium avocado",["avocado","avocados"]),
("pineapple_raw","Pineapple, raw","Fruit",50,0.54,13.12,0.12,165,"1 cup chunks",["pineapple"]),
("watermelon_raw","Watermelon, raw","Fruit",30,0.61,7.55,0.15,152,"1 cup pieces",["watermelon"]),
("pear_raw","Pear, raw","Fruit",57,0.36,15.23,0.14,178,"1 medium pear",["pear","pears"]),
("kiwi_raw","Kiwi, raw","Fruit",61,1.14,14.66,0.52,69,"1 medium kiwi",["kiwi","kiwifruit"]),

("spinach_raw","Spinach, raw","Vegetables",23,2.86,3.63,0.39,30,"1 cup",["spinach"]),
("broccoli_cooked","Broccoli, cooked","Vegetables",35,2.38,7.18,0.41,156,"1 cup chopped",["broccoli","cooked broccoli"]),
("carrots_raw","Carrots, raw","Vegetables",41,0.93,9.58,0.24,61,"1 medium carrot",["carrot","carrots"]),
("cucumber_raw","Cucumber, raw, with peel","Vegetables",15,0.65,3.63,0.11,52,"½ cup slices",["cucumber","cucumbers"]),
("tomato_raw","Tomato, raw","Vegetables",18,0.88,3.89,0.20,123,"1 medium tomato",["tomato","tomatoes"]),
("romaine_lettuce_raw","Romaine lettuce, raw","Vegetables",17,1.23,3.29,0.30,47,"1 cup shredded",["romaine lettuce","lettuce"]),
("red_bell_pepper_raw","Red bell pepper, raw","Vegetables",31,0.99,6.03,0.30,119,"1 medium pepper",["bell pepper","red pepper","capsicum"]),
("cauliflower_cooked","Cauliflower, cooked","Vegetables",23,1.84,4.11,0.45,124,"1 cup",["cauliflower"]),
("mushrooms_cooked","Mushrooms, cooked","Vegetables",28,2.17,5.29,0.47,78,"½ cup",["mushrooms","mushroom"]),
("onion_raw","Onion, raw","Vegetables",40,1.10,9.34,0.10,110,"1 medium onion",["onion","onions"]),
("zucchini_cooked","Zucchini, cooked","Vegetables",15,1.14,2.69,0.36,180,"1 cup",["zucchini","courgette"]),

("black_beans_cooked","Black beans, cooked","Legumes",132,8.86,23.71,0.54,172,"1 cup",["black beans","cooked black beans"]),
("kidney_beans_cooked","Kidney beans, cooked","Legumes",127,8.67,22.80,0.50,177,"1 cup",["kidney beans","red kidney beans"]),
("chickpeas_cooked","Chickpeas, cooked","Legumes",164,8.86,27.42,2.59,164,"1 cup",["chickpeas","chickpea","garbanzo beans"]),
("lentils_cooked","Lentils, cooked","Legumes",116,9.02,20.13,0.38,198,"1 cup",["lentils","lentil"]),
("green_peas_cooked","Green peas, cooked","Legumes",84,5.36,15.63,0.22,160,"1 cup",["green peas","peas"]),
("edamame_cooked","Edamame, cooked","Legumes",121,11.91,8.91,5.20,155,"1 cup",["edamame"]),
("hummus","Hummus","Legumes",166,7.90,14.30,9.60,30,"2 tbsp",["hummus"]),

("almonds_raw","Almonds, raw","Nuts, seeds & oils",579,21.15,21.55,49.93,28,"1 oz handful",["almonds","almond"]),
("walnuts_raw","Walnuts","Nuts, seeds & oils",654,15.23,13.71,65.21,28,"1 oz handful",["walnuts","walnut"]),
("peanut_butter","Peanut butter","Nuts, seeds & oils",588,25.09,20.00,50.39,16,"1 tbsp",["peanut butter"]),
("olive_oil","Olive oil","Nuts, seeds & oils",884,0.00,0.00,100.00,14,"1 tbsp",["olive oil"]),
("butter","Butter, salted","Nuts, seeds & oils",717,0.85,0.06,81.11,14,"1 tbsp",["butter"]),
("chia_seeds","Chia seeds, dried","Nuts, seeds & oils",486,16.54,42.12,30.74,12,"1 tbsp",["chia seeds","chia"]),
("dark_chocolate","Dark chocolate, 70–85% cacao","Nuts, seeds & oils",598,7.79,45.90,42.63,20,"2 small squares",["dark chocolate"]),
]

FOOD_CATALOG = [
    {
        "id": r[0], "name": r[1], "category": r[2],
        "kcal_per_100g": r[3], "protein_per_100g": r[4],
        "carbs_per_100g": r[5], "fat_per_100g": r[6],
        "serving_g": r[7], "serving_label": r[8], "aliases": r[9],
    }
    for r in _ROWS
]
FOOD_BY_ID = {food["id"]: food for food in FOOD_CATALOG}
QUICK_FOODS = (
    "egg_boiled", "banana_raw", "apple_raw", "white_rice_cooked",
    "chicken_breast_cooked", "greek_yogurt_plain", "salmon_cooked",
    "broccoli_cooked",
)


def _normalise(value):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


_ALIAS_MAP = {}
for _food in FOOD_CATALOG:
    for _alias in [_food["id"], _food["name"], *_food["aliases"]]:
        _normal = _normalise(_alias)
        if _normal:
            _ALIAS_MAP.setdefault(_normal, _food["id"])


def resolve_food(value):
    """Resolve exact catalogue names/aliases; never guess by substring."""
    return _ALIAS_MAP.get(_normalise(value))
