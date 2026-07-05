# -*- coding: utf-8 -*-
"""Demo data seeder for MenQ — talks to the running app over its API.
Run the app first (start.bat), then:  python seed_demo.py
Shows the generic model: any-store products with custom attributes, purchases,
multi-shop transfers, sales (retail/delivery/credit), a return, and cash."""
import json
import urllib.request
import urllib.error
import http.cookiejar

B = "http://127.0.0.1:8765"
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def post(path, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(B + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.load(_opener.open(req))
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()}


def get(path):
    return json.load(_opener.open(B + path))


def ref(entity, obj):
    return post(f"/api/ref?entity={entity}", obj)


print("Logging in as admin...")
if post("/api/login", {"username": "admin", "password": "admin"}).get("error"):
    print("LOGIN FAILED — reset admin password or edit this script.")
    raise SystemExit(1)

print("Settings + custom attributes...")
post("/api/settings", {"shop_name": "Իմ խանութը / My Store", "currency": "֏",
                       "bonus_percent": "3", "tax_percent": "0",
                       "attrs_def": [{"key": "size", "hy": "Չափս", "en": "Size"},
                                     {"key": "color", "hy": "Գույն", "en": "Color"}]})

for c in ["Հագուստ", "Կոշիկ", "Տեխնիկա", "Մթերք", "Կենցաղ"]:
    ref("categories", {"name": c})
for br in ["Nike", "Adidas", "Samsung", "Apple", "Ariana", "Grand Candy"]:
    ref("brands", {"name": br})
for s in [{"name": "Ալֆա ՍՊԸ", "phone": "010-11-22-33", "contact": "Արամ"},
          {"name": "BestTech", "phone": "060-70-80-90", "contact": "Կարեն"},
          {"name": "Գյումրի Մատակարար", "phone": "0312-5-66-77", "contact": "Վահե"}]:
    ref("suppliers", s)
for s in [{"name": "Կենտրոն մասնաճյուղ", "address": "Աբովյան 12", "phone": "010-55-44-33", "contact": "Արամ"},
          {"name": "Արաբկիր", "address": "Կոմիտաս 48", "phone": "010-23-23-23", "contact": "Նարե"}]:
    ref("shops", s)
for c in [{"name": "Դավիթ", "phone": "099-11-22-33"}, {"name": "Արմեն", "phone": "077-44-55-66"}]:
    ref("couriers", c)
ref("cars", {"courier": "Դավիթ", "make": "Toyota", "model": "Hilux", "plate": "35 LL 555"})
for cu in [{"name": "Աննա", "phone": "055-10-10-10"}, {"name": "Կարեն", "phone": "098-20-20-20"}]:
    ref("customers", cu)

print("Products (mixed store)...")
# (name, brand, category, size, color, cost, sell, wholesale, qty, min, unit, supplier)
P = [
    ("Կիսակոշիկ Air Max", "Nike", "Կոշիկ", "42", "Սև", 18000, 32000, 26000, 8, 2, "հատ", "Ալֆա ՍՊԸ"),
    ("Կիսակոշիկ Air Max", "Nike", "Կոշիկ", "43", "Սև", 18000, 32000, 26000, 3, 2, "հատ", "Ալֆա ՍՊԸ"),
    ("Սպորտային վերնաշապիկ", "Adidas", "Հագուստ", "L", "Կապույտ", 4000, 9000, 7000, 15, 3, "հատ", "Ալֆա ՍՊԸ"),
    ("Սպորտային վերնաշապիկ", "Adidas", "Հագուստ", "M", "Սպիտակ", 4000, 9000, 7000, 12, 3, "հատ", "Ալֆա ՍՊԸ"),
    ("Հեռախոս Galaxy A55", "Samsung", "Տեխնիկա", "", "", 95000, 135000, 125000, 6, 1, "հատ", "BestTech"),
    ("Ականջակալ Buds", "Samsung", "Տեխնիկա", "", "Սպիտակ", 18000, 29000, 25000, 10, 2, "հատ", "BestTech"),
    ("iPhone 15 պատյան", "Apple", "Տեխնիկա", "", "Սև", 3000, 8000, 6000, 20, 5, "հատ", "BestTech"),
    ("Կաթ 3.2%", "Ariana", "Մթերք", "", "", 350, 550, 480, 40, 10, "հատ", "Գյումրի Մատակարար"),
    ("Շոկոլադ", "Grand Candy", "Մթերք", "", "", 600, 1200, 950, 50, 10, "հատ", "Գյումրի Մատակարար"),
    ("Լվացքի փոշի 3կգ", "Ariana", "Կենցաղ", "", "", 2200, 3900, 3300, 18, 4, "հատ", "Գյումրի Մատակարար"),
]
ids = []
for (nm, br, cat, sz, col, c, s, w, q, mn, unit, sup) in P:
    attrs = {}
    if sz:
        attrs["size"] = sz
    if col:
        attrs["color"] = col
    r = post("/api/products", {"name": nm, "brand": br, "category": cat, "attrs": attrs,
                               "unit": unit, "cost_price": c, "sell_price": s,
                               "wholesale_price": w, "quantity": q, "min_qty": mn,
                               "supplier": sup})
    ids.append(r.get("id"))
pid = lambda n: ids[n - 1]

print("Purchase (supplier, credit -> payable)...")
post("/api/purchase", {"supplier": "BestTech", "payment": "credit", "paid": 200000,
                       "items": [{"product_id": pid(5), "qty": 4, "unit_cost": 94000}]})

print("Transfers (warehouse -> shops)...")
post("/api/transfer", {"product_id": pid(1), "from_loc": "Պահեստ", "to_loc": "Կենտրոն մասնաճյուղ", "qty": 3})
post("/api/transfer", {"product_id": pid(3), "from_loc": "Պահեստ", "to_loc": "Արաբկիր", "qty": 5})

print("Sales (retail cash, from-shop, delivery credit)...")
post("/api/sale", {"items": [{"product_id": pid(8), "qty": 3}, {"product_id": pid(9), "qty": 2}],
                   "type": "retail", "payment": "cash", "customer": "Աննա"})
post("/api/sale", {"items": [{"product_id": pid(1), "qty": 1}], "location": "Կենտրոն մասնաճյուղ",
                   "type": "retail", "payment": "cash"})
post("/api/sale", {"items": [{"product_id": pid(3), "qty": 4}], "location": "Արաբկիր",
                   "type": "retail", "payment": "cash", "customer": "Կարեն"})
post("/api/sale", {"items": [{"product_id": pid(5), "qty": 2}], "type": "delivery",
                   "price_mode": "wholesale", "payment": "credit",
                   "courier": "Դավիթ", "shop": "Կենտրոն մասնաճյուղ", "paid": 100000})
post("/api/sale", {"items": [{"product_id": pid(6), "qty": 3}], "type": "retail", "payment": "transfer"})

print("A return (refund)...")
post("/api/return", {"items": [{"product_id": pid(9), "qty": 1, "unit_price": 1200}],
                     "payment": "cash"})

print("Cash box expenses...")
post("/api/cashbox", {"kind": "out", "amount": 30000, "reason": "Վարձ / Rent"})
post("/api/cashbox", {"kind": "out", "amount": 12000, "reason": "Կոմունալ"})

print("DONE. Open http://127.0.0.1:8765 and refresh.")
