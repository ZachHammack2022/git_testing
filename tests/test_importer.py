from recipe_planner import importer

PAGE = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"WebSite","name":"Site"},
 {"@type":["Recipe"],"name":"Lemon &amp; Garlic Shrimp","description":"<p>Bright and quick.</p>",
  "recipeYield":["4","4 servings"],"totalTime":"PT1H5M","recipeCuisine":["Italian"],
  "keywords":"quick, seafood",
  "recipeIngredient":["1 lb large shrimp, peeled","3 cloves garlic","1 lemon","2 tbsp butter"],
  "recipeInstructions":[{"@type":"HowToSection","name":"Prep","itemListElement":[
      {"@type":"HowToStep","text":"Peel the shrimp."}]},
    {"@type":"HowToStep","text":"Cook garlic in butter."},"Add shrimp and lemon."]}
]}</script></head><body>hi</body></html>"""


def test_parse_jsonld():
    r = importer.parse_jsonld(PAGE)
    assert r["title"] == "Lemon & Garlic Shrimp"
    assert r["description"] == "Bright and quick."
    assert r["servings"] == 4 and r["prep_minutes"] == 65
    assert r["cuisine"] == "Italian" and r["protein"] == "shrimp"
    assert r["ingredients"][0]["unit"] == "lb" and r["ingredients"][1]["unit"] == "clove"
    assert r["steps"] == ["Peel the shrimp.", "Cook garlic in butter.", "Add shrimp and lemon."]
    assert r["tags"] == ["quick", "seafood"]


def test_no_recipe():
    assert importer.parse_jsonld("<html><script type='application/ld+json'>{bad json</script></html>") is None
    assert importer.parse_jsonld("<html></html>") is None


def test_iso_minutes_and_protein():
    assert importer._iso_minutes("PT45M") == 45
    assert importer._iso_minutes("PT2H") == 120
    assert importer.guess_protein(["olive oil", "chickpeas"]) == "vegetarian"
    assert importer.guess_protein(["boneless chicken thighs"]) == "chicken"


def test_page_text_strips_scripts():
    assert "var x" not in importer.page_text("<p>Hello</p><script>var x=1</script>")


def test_import_route(client, monkeypatch):
    monkeypatch.setattr(importer, "fetch", lambda url: PAGE)
    page = client.post("/recipes/import-url", data={"url": "https://example.com/r"}).get_data(as_text=True)
    assert "Lemon &amp; Garlic Shrimp" in page and "1 lb large shrimp" in page


def test_fetch_rejects_non_http():
    import pytest
    with pytest.raises(importer.RecipeImportError):
        importer.fetch("file:///etc/passwd")
