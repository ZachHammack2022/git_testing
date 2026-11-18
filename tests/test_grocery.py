from recipe_planner import grocery


def test_normalize_name():
    assert grocery.normalize_name("Yellow Onions") == "yellow onion"
    assert grocery.normalize_name("tomatoes") == "tomato"
    assert grocery.normalize_name("Garlic, minced") == "garlic"
    assert grocery.normalize_name("asparagus") == "asparagus"
    assert grocery.normalize_name("chickpeas") == "chickpeas"
    assert grocery.normalize_name("molasses") == "molasses"


def test_parse_quantity():
    assert grocery.parse_quantity("1 1/2") == 1.5
    assert grocery.parse_quantity("3/4") == 0.75
    assert grocery.parse_quantity(2) == 2.0
    assert grocery.parse_quantity("") == 0.0


def test_parse_ingredient_line():
    i = grocery.parse_ingredient_line("1 1/2 cups jasmine rice")
    assert i["quantity"] == 1.5 and i["unit"] == "cup" and i["name"] == "jasmine rice"
    i = grocery.parse_ingredient_line("2 cloves garlic")
    assert i["unit"] == "clove" and i["name"] == "garlic" and i["category"] == "produce"
    i = grocery.parse_ingredient_line("- 3 eggs")
    assert i["quantity"] == 3 and i["unit"] == "" and i["name"] == "eggs"
    assert grocery.parse_ingredient_line("   ") is None
    i = grocery.parse_ingredient_line("salt")
    assert i["quantity"] == 0 and i["name"] == "salt"


def test_guess_category_prefers_longest_match():
    assert grocery.guess_category("sweet potatoes") == "produce"
    assert grocery.guess_category("chicken thighs") == "meat & seafood"
    assert grocery.guess_category("black pepper") == "spices"
    assert grocery.guess_category("cheddar cheese") == "dairy & eggs"


def _meal(ings, servings=4, recipe_servings=4, title="R"):
    return {"servings": servings, "recipe": {"title": title, "servings": recipe_servings, "ingredients": ings}}


def test_aggregates_and_converts_units():
    meals = [
        _meal([{"name": "olive oil", "quantity": 2, "unit": "tbsp"},
               {"name": "onions", "quantity": 1, "unit": ""}], title="A"),
        _meal([{"name": "Olive Oil", "quantity": 0.25, "unit": "cup"},
               {"name": "onion", "quantity": 2, "unit": ""}], title="B"),
    ]
    items = {i["key"]: i for i in grocery.build_shopping_list(meals)}
    # 2 tbsp + 1/4 cup = 6 + 12 tsp = 18 tsp = 3/8 cup -> shown as cups
    assert items["olive oil"]["amount"].endswith("cup")
    assert items["onion"]["amount"] == "3"
    assert items["onion"]["recipes"] == ["A", "B"]


def test_scales_by_servings():
    meals = [_meal([{"name": "chicken thighs", "quantity": 1, "unit": "lb"}], servings=2, recipe_servings=4)]
    items = grocery.build_shopping_list(meals)
    assert items[0]["amount"] == "8 oz"


def test_pantry_flagged():
    meals = [_meal([{"name": "salt", "quantity": 1, "unit": "tsp"}, {"name": "rice", "quantity": 1, "unit": "cup"}])]
    items = {i["key"]: i for i in grocery.build_shopping_list(meals, pantry=["Salt"])}
    assert items["salt"]["in_pantry"] and not items["rice"]["in_pantry"]
    assert "salt" not in grocery.as_text(list(items.values()))


def test_mixed_families_listed_separately():
    meals = [_meal([{"name": "garlic", "quantity": 3, "unit": "clove"}]),
             _meal([{"name": "garlic", "quantity": 1, "unit": "head"}])]
    item = grocery.build_shopping_list(meals)[0]
    assert "3 cloves" in item["amount"] and "1 head" in item["amount"]


def test_nice_number():
    assert grocery._nice_number(1.5) == "1 1/2"
    assert grocery._nice_number(0.33) == "1/3"
    assert grocery._nice_number(2.0) == "2"
    assert grocery._nice_number(0.97) == "1"


def test_count_items_round_up():
    meals = [_meal([{"name": "avocado", "quantity": 1, "unit": ""},
                    {"name": "coconut milk", "quantity": 1, "unit": "can"}], servings=2, recipe_servings=4)]
    items = {i["key"]: i for i in grocery.build_shopping_list(meals)}
    assert items["avocado"]["amount"] == "1"
    assert items["coconut milk"]["amount"] == "1 can"


def test_ingredient_line_round_trip():
    for line in ["2 cups chicken broth", "1 1/2 cloves garlic", "3 bunches cilantro", "1 tbsp honey", "salt"]:
        ing = grocery.parse_ingredient_line(line)
        assert grocery.ingredient_to_line(ing) == line
