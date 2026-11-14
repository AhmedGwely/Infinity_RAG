import os, tempfile, unittest
from pathlib import Path

from infinity.chunking import locator_label, split_text
from infinity.crypto import SecretBox, hash_password, mask_secret, password_problems, verify_password
from infinity.fusion import reciprocal_rank_fusion
from infinity.netguard import UnsafeURL, assert_safe_url
from infinity.parsers import UnsupportedFormat, parse_file
from infinity.htmltext import html_to_text


class CryptoTests(unittest.TestCase):
    def test_password_roundtrip(self):
        stored = hash_password("Correct-Horse-9")
        self.assertTrue(verify_password("Correct-Horse-9", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("x", "garbage"))

    def test_password_policy(self):
        self.assertTrue(password_problems("short"))
        self.assertEqual(password_problems("GoodPassw0rd"), [])

    def test_secretbox(self):
        box = SecretBox("0123456789abcdef-secret")
        token = box.encrypt("sk-live-123")
        self.assertNotIn("sk-live", token)
        self.assertEqual(box.decrypt(token), "sk-live-123")
        with self.assertRaises(ValueError):
            SecretBox("another-secret-value-xyz").decrypt(token)
        with self.assertRaises(ValueError):
            SecretBox("short")
        self.assertTrue(mask_secret("abcdefghijkl").endswith("ijkl"))


class NetguardTests(unittest.TestCase):
    def test_blocks_private_and_internal(self):
        for url in ["http://127.0.0.1/", "http://10.1.2.3/x", "http://192.168.0.5", "http://169.254.169.254/latest",
                    "http://localhost:6333", "http://host.docker.internal:11434", "http://postgres:5432", "ftp://example.com", "http://[::1]/"]:
            with self.assertRaises(UnsafeURL, msg=url):
                assert_safe_url(url)

    def test_allows_public_literal_and_override(self):
        self.assertEqual(assert_safe_url("http://93.184.216.34/"), "http://93.184.216.34/")
        self.assertEqual(assert_safe_url("http://10.0.0.5/", allow_private=True), "http://10.0.0.5/")


class FusionTests(unittest.TestCase):
    def test_rrf_prefers_agreement(self):
        fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "b", "d"]])
        ids = [i for i, _ in fused]
        self.assertEqual(set(ids[:2]), {"b", "c"})  # present in both lists
        self.assertEqual(set(ids), {"a", "b", "c", "d"})

    def test_duplicates_in_one_list_counted_once(self):
        fused = dict(reciprocal_rank_fusion([["a", "a", "a"]]))
        self.assertAlmostEqual(fused["a"], 1 / 61)


class SplitTests(unittest.TestCase):
    def test_respects_size_and_keeps_content(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(30))
        chunks = split_text(text, size=800, overlap=100)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 800 for c in chunks))
        self.assertIn("Paragraph 29", chunks[-1])

    def test_oversized_paragraph_is_split(self):
        chunks = split_text("x" * 5000, size=1000, overlap=100)
        self.assertGreaterEqual(len(chunks), 5)

    def test_labels(self):
        self.assertIn("printed 10", locator_label({"type": "pdf", "pdf_pages": [12], "printed_pages": [10]}))
        self.assertEqual(locator_label({"type": "pptx", "slide": 4}), "Slide 4")
        self.assertIn("Sheet 'S1'", locator_label({"type": "xlsx", "sheet": "S1", "row_start": 2, "row_end": 9}))


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def p(self, name):
        return Path(self.dir.name) / name

    def test_docx_headings_and_tables(self):
        from docx import Document
        d = Document()
        d.add_heading("Communication Interfaces", level=1)
        d.add_paragraph("The meter supports an optical port and RS-232.")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "Port", "Speed"
        t.cell(1, 0).text, t.cell(1, 1).text = "Optical", "9600"
        d.add_heading("Accuracy", level=1)
        d.add_paragraph("Class 0.2S per IEC 62053-22.")
        d.save(self.p("m.docx"))
        doc = parse_file(self.p("m.docx"))
        self.assertEqual(doc.doc_type, "docx")
        sections = {c.section for c in doc.chunks}
        self.assertEqual(sections, {"Communication Interfaces", "Accuracy"})
        self.assertTrue(any("| Optical | 9600 |" in c.text for c in doc.chunks))

    def test_xlsx_rows_and_locator(self):
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "Specs"
        ws.append(["Model", "Class"])
        for i in range(60):
            ws.append([f"M{i}", "0.2S"])
        wb.save(self.p("s.xlsx"))
        doc = parse_file(self.p("s.xlsx"))
        self.assertEqual(len(doc.chunks), 3)
        self.assertEqual(doc.chunks[0].locator, {"type": "xlsx", "sheet": "Specs", "row_start": 2, "row_end": 26})
        self.assertIn("| Model | Class |", doc.chunks[0].text)
        self.assertIn("M59", doc.chunks[-1].text)

    def test_pptx_slides(self):
        from pptx import Presentation
        prs = Presentation()
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = "MTS 320"
        s.placeholders[1].text = "Accuracy 0.02 percent"
        prs.save(self.p("d.pptx"))
        doc = parse_file(self.p("d.pptx"))
        self.assertEqual(doc.chunks[0].locator, {"type": "pptx", "slide": 1})
        self.assertIn("0.02", doc.chunks[0].text)

    def test_csv_and_text_and_arabic(self):
        self.p("a.csv").write_text("id,name\n1,Alpha\n2,Beta\n", encoding="utf-8")
        doc = parse_file(self.p("a.csv"))
        self.assertEqual(doc.chunks[0].locator["type"], "csv")
        self.p("n.txt").write_text("عداد ذكي من الفئة 0.2S\n\nنص ثانٍ", encoding="utf-8")
        doc = parse_file(self.p("n.txt"))
        self.assertIn("عداد", doc.chunks[0].text)

    def test_unsupported_and_empty(self):
        self.p("x.exe").write_bytes(b"MZ")
        with self.assertRaises(UnsupportedFormat):
            parse_file(self.p("x.exe"))
        self.p("e.txt").write_text("   ")
        self.assertTrue(parse_file(self.p("e.txt")).issues)


class HtmlTests(unittest.TestCase):
    def test_extracts_text_links_title(self):
        html = """<html><head><title>MK6 Meter</title></head><body><nav>menu</nav>
        <main><h1>Mk6N</h1><p>Class 0.2S meter.</p><ul><li>Optical port</li></ul>
        <table><tr><th>Item</th><th>Value</th></tr><tr><td>Class</td><td>0.2S</td></tr></table>
        <a href="/docs/datasheet.pdf#x">Datasheet</a><a href="mailto:a@b.c">m</a></main>
        <script>evil()</script></body></html>"""
        title, text, links = html_to_text(html, "https://example.com/p/")
        self.assertEqual(title, "MK6 Meter")
        self.assertIn("Class 0.2S meter.", text)
        self.assertIn("Class | 0.2S", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("menu", text)
        self.assertEqual(links, ["https://example.com/docs/datasheet.pdf"])


if __name__ == "__main__":
    unittest.main()
