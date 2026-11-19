import os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for k in ("GEMINI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(k, None)
from app import brain, db, llm, store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        os.environ["DB_PATH"] = self.tmp.name
        db.init_db()
    def tearDown(self):
        os.unlink(self.tmp.name)
    def ask(self, t):
        return brain.handle_message(t, "test")


class TestBrain(Base):
    def test_price(self):
        r = self.ask("How much are the silver hoops?")
        self.assertEqual(r["status"], "answered"); self.assertIn("799", r["reply"])
    def test_typo(self):
        self.assertIn("799", self.ask("silvr hoops price")["reply"])
    def test_stock_zero_changes_reply(self):
        db.set_stock(1, 0)
        self.assertIn("sold out", self.ask("price of silver hoops")["reply"])
    def test_low_stock_handoff(self):
        r = self.ask("Is the kurta in stock?")
        self.assertEqual((r["status"], r["reason"]), ("handoff", "low_stock"))
        self.assertEqual(len(db.list_queue()), 1)
    def test_sold_out_product(self):
        self.assertIn("sold out", self.ask("Is the lip balm in stock?")["reply"])
    def test_refund_handoff_and_resolve(self):
        r = self.ask("I want a refund")
        self.assertEqual(r["status"], "handoff")
        qid = db.list_queue()[0]["id"]
        self.assertTrue(db.resolve_queue(qid)); self.assertEqual(db.list_queue(), [])
    def test_delivery_city(self):
        r = self.ask("Do you deliver to Jaipur?")
        self.assertIn("Jaipur", r["reply"]); self.assertIn("2 to 4", r["reply"])
    def test_delivery_unknown_city(self):
        self.assertEqual(self.ask("Do you deliver to Goa?")["reason"], "city_not_covered")
    def test_delivery_no_city_asks(self):
        self.assertEqual(self.ask("Do you deliver?")["reason"], "clarify_city")
    def test_return_policy(self):
        self.assertIn("7 days", self.ask("What is your return policy?")["reply"])
    def test_no_match(self):
        self.assertEqual(self.ask("do you sell laptops")["reason"], "no_match")
    def test_ambiguous(self):
        db.upsert_product("Linen Shirt", 999, 7, "linen,shirt")
        r = self.ask("linen price")
        self.assertEqual(r["reason"], "clarify_product"); self.assertIn("Linen Shirt", r["reply"])
    def test_greeting_and_empty(self):
        self.assertEqual(self.ask("hi")["reason"], "greeting")
        self.assertEqual(self.ask("   ")["reason"], "empty")
    def test_logs(self):
        self.ask("price of tote"); self.assertEqual(len(db.list_chats()), 1)


class TestNumberCheck(Base):
    def setUp(self):
        super().setUp()
        self._a, self._c = llm.available, llm.call_llm
        llm.available = lambda: True
    def tearDown(self):
        llm.available, llm.call_llm = self._a, self._c
        super().tearDown()
    def test_wrong_price_blocked(self):
        llm.call_llm = lambda p, system=None: "The Silver Hoop Earrings are only \u20b9500!"
        r = self.ask("price of silver hoops")
        self.assertEqual((r["status"], r["reason"]), ("handoff", "number_check_failed"))
    def test_friendly_rewrite_allowed(self):
        llm.call_llm = lambda p, system=None: "Great choice! The Silver Hoop Earrings are \u20b9799."
        r = self.ask("price of silver hoops")
        self.assertEqual(r["status"], "answered"); self.assertIn("Great choice", r["reply"])
    def test_llm_handoff(self):
        llm.call_llm = lambda p, system=None: "HANDOFF"
        self.assertEqual(self.ask("price of silver hoops")["reason"], "llm_handoff")
    def test_check_function(self):
        self.assertTrue(brain.number_check("It is \u20b91,299.", "costs \u20b91,299 (1299)"))
        self.assertFalse(brain.number_check("It is \u20b91,200.", "costs \u20b91,299 (1299)"))


class TestStore(Base):
    def test_csv(self):
        rows = store.parse_csv('Name,Price,Stock\nTea,"\u20b91,200",5\n,5,5\nBad,x,1\n')
        self.assertEqual(rows, [{"name": "Tea", "price": 1200, "stock": 5, "aliases": "", "sizes": ""}])
    def test_sheet_url(self):
        self.assertIn("export?format=csv&gid=7", store.sheet_csv_url("https://docs.google.com/spreadsheets/d/abc_1/edit#gid=7"))
    def test_jsonld(self):
        html = '<script type="application/ld+json">{"@type":"Product","name":"Mug","offers":{"price":"250","availability":"https://schema.org/OutOfStock"}}</script>'
        self.assertEqual(store.parse_jsonld(html)[0]["stock"], 0)
    def test_upsert_updates_stock(self):
        db.upsert_product("Linen Kurta", 1299, 9)
        self.assertEqual(self.ask("price of kurta")["status"], "answered")
    def test_blocks_private(self):
        with self.assertRaises(store.StoreError): store.import_store("http://127.0.0.1/data.csv")


if __name__ == "__main__":
    unittest.main()
