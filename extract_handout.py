import glob
import json
import os
import re
import sqlite3
import pdfplumber
import time

from google import genai
from dotenv import load_dotenv

from model import extract_courses
#from model import make_client

load_dotenv()
#MODEL = "qwen2.5:14b"
PDF_DIR = "pdfs"                 # folder containing your PDFs
PDF_PATH = "dataset/handouts/003_BIO_F211.pdf"
DB_PATH = "courses.db"
MAX_CHARS_PER_CALL = 30000       # big PDFs are sent to the model in windows
OVERLAP_BLOCKS = 3   

COLS = [ "course_title", "attendance_policy",
        "makeup_policy", "syllabus"]
CORE_FIELDS = ["course_title", "syllabus"]
CONF_RANK = {"low": 0, "medium": 1, "high": 2}

def read_pdf(path):
    """Return a single continuous string with [page N] prepended to every page's content."""
    page_blocks = []
    
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            content_parts = []
            
            # 1. Extract plain text
            text = page.extract_text()
            if text and text.strip():
                content_parts.append(text.strip())
            
            # 2. Extract tables as pipe-separated text
            try:
                tables = page.extract_tables() or []
            except Exception:
                tables = []
                
            for table in tables:
                table_lines = []
                for row in table:
                    row_str = " | ".join(str(cell).strip() if cell is not None else "" for cell in row)
                    if row_str.replace("|", "").strip():
                        table_lines.append(row_str)
                if table_lines:
                    content_parts.append("\n".join(table_lines))
            
            # 3. Assemble page content under its page header
            page_content = "\n\n".join(content_parts) if content_parts else "[Empty Page]"
            page_blocks.append(f"[page {i}]\n{page_content}")
            
    return "\n\n".join(page_blocks)


def normalize_course_code(raw):
    
    if not raw:
        return None, False
    s = re.sub(r"[^A-Za-z0-9]", "", str(raw)).upper()   #remove all spaces,dashes exept letters and numbers
    m = re.match(r"^([A-Z]{2,4}?)([FGHPTU]?\d{3,4}[A-Z]?)$", s)   #split into two groups CS+F211
    if not m:
        return str(raw).strip(), False          # keep raw text, but flag for verification
    return f"{m.group(1)} {m.group(2)}", True



def init_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_code TEXT UNIQUE,
        course_title TEXT, credits REAL,
        attendance_policy TEXT, midsem TEXT, compre TEXT, makeup_policy TEXT, syllabus TEXT,
        -- source metadata
        source_document TEXT, page_reference TEXT, extraction_confidence TEXT,
        -- verification
        unreliable_fields TEXT DEFAULT '[]',
        fields_to_verify TEXT DEFAULT '[]',
        needs_verification INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS evaluation_components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        name TEXT, weightage_percent REAL, duration TEXT, date TEXT, nature TEXT
    );
    """)
    return conn

def save_course(conn, c, source_doc):
    """Insert a course, or merge into an existing row with the same course code."""
    code, code_ok = normalize_course_code(c.get("course_code"))
    new = {k: (c.get(k) if c.get(k) not in ("", []) else None) for k in COLS}
    new_unreliable = set(c.get("unreliable_fields") or [])
    if not code_ok:
        new_unreliable.add("course_code")
    new_conf = c.get("confidence", "low")
    new_pages = c.get("page_reference")
 
    row = conn.execute("SELECT * FROM courses WHERE course_code = ?", (code,)).fetchone() if code else None
 
    if row:                                   # merge: keep existing values, fill the gaps
        data = {k: row[k] if row[k] is not None else new[k] for k in COLS}
        unreliable = set(json.loads(row["unreliable_fields"])) | new_unreliable
        conf = min(row["extraction_confidence"] or "low", new_conf, key=lambda x: CONF_RANK[x])
        pages = ", ".join(dict.fromkeys(p for p in [row["page_reference"], new_pages] if p))
        src = row["source_document"]
        course_id = row["id"]
    else:
        data, unreliable, conf, pages, src, course_id = new, new_unreliable, new_conf, new_pages, source_doc, None
 
    # evaluation components
    comps = c.get("evaluation_components") or []
    existing_names = set()
    if course_id:
        existing_names = {r["name"] for r in conn.execute(
            "SELECT name FROM evaluation_components WHERE course_id = ?", (course_id,))}
 
    # what must be checked by a human: declared-unreliable + missing core fields
    missing = {f for f in CORE_FIELDS if data[f] is None}
    if not comps and not existing_names:
        missing.add("evaluation_components")
    to_verify = sorted(unreliable | missing)
    needs = int(bool(to_verify) or conf == "low")
 
    values = dict(data, course_code=code, source_document=src, page_reference=pages,
                  extraction_confidence=conf, unreliable_fields=json.dumps(sorted(unreliable)),
                  fields_to_verify=json.dumps(to_verify), needs_verification=needs)
 
    if course_id:
        sets = ", ".join(f"{k} = :{k}" for k in values)
        conn.execute(f"UPDATE courses SET {sets} WHERE id = :id", dict(values, id=course_id))
    else:
        cols = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        course_id = conn.execute(f"INSERT INTO courses ({cols}) VALUES ({params})", values).lastrowid
 
    for comp in comps:
        if comp.get("name") and comp["name"] not in existing_names:
            conn.execute(
                "INSERT INTO evaluation_components (course_id, name, weightage_percent, duration, date, nature) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (course_id, comp["name"], comp.get("weightage_percent"), comp.get("duration"),
                 comp.get("date"), comp.get("nature")))
            existing_names.add(comp["name"])


def main():
    #client = make_client()
    client = genai.Client()
    conn = init_db(DB_PATH)
 
    
    name = os.path.basename(PDF_PATH)
    t = time.time()
    text = read_pdf(PDF_PATH)
    print(f"pdf parse: {time.time() - t:.1f}s, {len(text)} chars")
    
    
   
    for course in extract_courses(client, text):
        save_course(conn, course, name)
    conn.commit()
     
    total = conn.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
    flagged = conn.execute("SELECT COUNT(*) FROM courses WHERE needs_verification = 1").fetchone()[0]
    print(f"\nDone. {total} courses in '{DB_PATH}' ({flagged} flagged for verification).")
 
 
if __name__ == "__main__":
    main()