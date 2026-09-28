import json
import os
import re
import sqlite3

import pandas as pd
import pdfplumber

# --------------------------------------------------------------------------
# PDF column positions (the timetable has a fixed 14-column layout)
# --------------------------------------------------------------------------
COL_COM_COD = 0
COL_COURSE_NO = 1
COL_TITLE = 2  # also holds the "Tutorial" / "Practical" label on section rows
COL_L, COL_P, COL_T, COL_S, COL_UCH = 3, 4, 5, 6, 7
COL_SEC = 8
COL_INSTRUCTOR = 9
COL_ROOM = 10
COL_DAYS = 11
COL_MIDSEM = 12
COL_COMPRE = 13
N_COLS = 14

# Final table layout: one row per COM_COD
DB_COLUMNS = [
    "COM_COD",  # unique identifier (PRIMARY KEY)
    "COURSE_NO",
    "COURSE_TITLE",
    "CREDIT_L",
    "CREDIT_P",
    "CREDIT_T",
    "CREDIT_S",
    "CREDIT_UCH",
    "COURSE_IC",  # instructor-in-charge
    "MIDSEM_DATE_SESSION",
    "COMPRE_DATE_SESSION",
    "SECTIONS",  # e.g. "L1 | L2 | T1"
    "INSTRUCTORS",  # one entry per section, same order as SECTIONS
    "ROOMS",
    "DAYS_HOURS",
    "SECTIONS_JSON",  # full structured detail for every section
    "SOURCE_LOCATION",
]


def clean(cell) -> str:
    """Strip a cell and collapse wrapped lines / repeated spaces into one space."""
    if not cell:
        return ""
    return re.sub(r"\s+", " ", cell).strip()


def is_header_row(row: list) -> bool:
    """True for the two header rows repeated at the top of every page."""
    joined = " ".join(row).upper()
    return (
        "COURSE NO" in joined
        or (row[COL_L] == "L" and row[COL_P] == "P" and row[COL_T] == "T")
    )


def section_kind(sec: str, label: str) -> str:
    """Lecture / Tutorial / Practical, from the label or the section prefix."""
    if label:
        return label
    return {"L": "Lecture", "T": "Tutorial", "P": "Practical"}.get(
        sec[:1].upper(), ""
    )


# --------------------------------------------------------------------------
# Step 1: PDF rows -> one dict per course
# --------------------------------------------------------------------------
def extract_courses(pdf_path: str) -> list:
    """
    Walk every table row in the PDF and group rows under the course they
    belong to. A new course starts on every row with a COM COD; a new section
    starts on every row with a SEC value; any other row (extra instructor
    lines, or rows at the top of a page) continues the section above it,
    even across page breaks.
    """
    pdf_filename = os.path.basename(pdf_path)
    courses = []
    course = None
    section = None
    orphans = 0

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            table = page.extract_table()
            if not table:
                continue

            for raw in table:
                if len(raw) < N_COLS:
                    continue
                row = [clean(c) for c in raw[:N_COLS]]

                if not any(row) or is_header_row(row):
                    continue

                is_course_row = bool(row[COL_COM_COD] or row[COL_COURSE_NO])

                # ---- new course --------------------------------------
                if is_course_row:
                    course = {
                        "COM_COD": row[COL_COM_COD],
                        "COURSE_NO": row[COL_COURSE_NO],
                        "COURSE_TITLE": row[COL_TITLE],
                        "CREDIT_L": row[COL_L],
                        "CREDIT_P": row[COL_P],
                        "CREDIT_T": row[COL_T],
                        "CREDIT_S": row[COL_S],
                        "CREDIT_UCH": row[COL_UCH],
                        # The instructor printed on the course's own row
                        # (in capitals in the PDF) is the instructor-in-charge
                        "COURSE_IC": row[COL_INSTRUCTOR],
                        "MIDSEM_DATE_SESSION": row[COL_MIDSEM],
                        "COMPRE_DATE_SESSION": row[COL_COMPRE],
                        "SOURCE_LOCATION": f"{pdf_filename} - Page {page_idx}",
                        "sections": [],
                    }
                    courses.append(course)
                    section = None

                # ---- row before any course (e.g. cut-off from prev. page)
                elif course is None:
                    orphans += 1
                    continue

                # ---- new section within the current course ------------
                if row[COL_SEC]:
                    section = {
                        "section": row[COL_SEC],
                        # On a course row the title column holds the course
                        # title; on other rows it holds "Tutorial"/"Practical"
                        "type": section_kind(
                            row[COL_SEC], "" if is_course_row else row[COL_TITLE]
                        ),
                        "instructors": [],
                        "room": row[COL_ROOM],
                        "days_hours": row[COL_DAYS],
                    }
                    course["sections"].append(section)

                # ---- continuation row: extra instructor / details ------
                if section is not None:
                    inst = row[COL_INSTRUCTOR]
                    if inst and inst not in section["instructors"]:
                        section["instructors"].append(inst)
                    if row[COL_ROOM] and not section["room"]:
                        section["room"] = row[COL_ROOM]
                    if row[COL_DAYS] and not section["days_hours"]:
                        section["days_hours"] = row[COL_DAYS]

                # exam dates only ever belong to the course, not a section
                if not course["MIDSEM_DATE_SESSION"] and row[COL_MIDSEM]:
                    course["MIDSEM_DATE_SESSION"] = row[COL_MIDSEM]
                if not course["COMPRE_DATE_SESSION"] and row[COL_COMPRE]:
                    course["COMPRE_DATE_SESSION"] = row[COL_COMPRE]

    if orphans:
        print(
            f"Note: ignored {orphans} row(s) that appear before any course "
            "(continuation of a course cut off before page 1)."
        )
    return courses


# --------------------------------------------------------------------------
# Step 2: course dicts -> flat DataFrame (one row per course)
# --------------------------------------------------------------------------
def courses_to_dataframe(courses: list) -> pd.DataFrame:
    sep = " | "
    records = []
    for c in courses:
        secs = c["sections"]
        rec = {k: v for k, v in c.items() if k != "sections"}
        rec["SECTIONS"] = sep.join(s["section"] for s in secs)
        rec["INSTRUCTORS"] = sep.join(", ".join(s["instructors"]) for s in secs)
        rec["ROOMS"] = sep.join(s["room"] for s in secs)
        rec["DAYS_HOURS"] = sep.join(s["days_hours"] for s in secs)
        rec["SECTIONS_JSON"] = json.dumps(secs, ensure_ascii=False)
        records.append(rec)

    df = pd.DataFrame(records, columns=DB_COLUMNS)

    # COM_COD is the unique identifier. If it ever repeats, keep the first.
    dupes = df["COM_COD"].duplicated(keep="first")
    if dupes.any():
        print(
            f"Warning: {dupes.sum()} repeated COM_COD value(s) dropped: "
            f"{sorted(df.loc[dupes, 'COM_COD'].unique())}"
        )
        df = df[~dupes]
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# Step 3: write to SQLite
# --------------------------------------------------------------------------
def ensure_table(conn: sqlite3.Connection, table_name: str):
    """Create the table; rebuild it if it still has an older/different layout."""
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table_name}");')
    existing = [r[1] for r in cur.fetchall()]

    if existing and existing != DB_COLUMNS:
        print(
            f"Table '{table_name}' has an older layout - dropping and rebuilding it."
        )
        cur.execute(f'DROP TABLE "{table_name}";')
        existing = []

    if not existing:
        cols_sql = ", ".join(
            f'"{c}" TEXT PRIMARY KEY' if c == "COM_COD" else f'"{c}" TEXT'
            for c in DB_COLUMNS
        )
        cur.execute(f'CREATE TABLE "{table_name}" ({cols_sql});')
    conn.commit()


def export_pdf_to_sqlite(
    pdf_path: str, db_path: str, table_name: str = "course_timetable"
):
    courses = extract_courses(pdf_path)
    if not courses:
        print("No table data found in PDF.")
        return

    df = courses_to_dataframe(courses)

    conn = sqlite3.connect(db_path)
    ensure_table(conn, table_name)

    # Upsert on COM_COD: new courses are added, existing ones are refreshed,
    # so re-running the script never creates duplicates.
    placeholders = ", ".join("?" for _ in DB_COLUMNS)
    col_list = ", ".join(f'"{c}"' for c in DB_COLUMNS)
    conn.executemany(
        f'INSERT OR REPLACE INTO "{table_name}" ({col_list}) VALUES ({placeholders});',
        df[DB_COLUMNS].values.tolist(),
    )
    conn.commit()
    conn.close()
    print(f"Wrote {len(df)} courses (one row each) to table '{table_name}'.")


if __name__ == "__main__":
    PDF_FILE = "dataset/timetable_cropped.pdf"
    DB_FILE = "course.db"
    TABLE_NAME = "course_timetable"

    export_pdf_to_sqlite(PDF_FILE, DB_FILE, TABLE_NAME)

'''
import os
import sqlite3
import pandas as pd
import pdfplumber


def clean_col_name(col_name: str) -> str:
    """Sanitize header string into a clean SQL column name."""
    if not col_name:
        return "UNNAMED_COLUMN"
    # Replace newlines, slashes, and special characters with underscores
    cleaned = (
        col_name.replace("\n", " ")
        .replace("/", "_")
        .replace("-", "_")
        .strip()
    )
    cleaned = "_".join(cleaned.split())
    return cleaned.upper()


def extract_pdf_tables(pdf_path: str) -> pd.DataFrame:
    """Extract all tables from multi-page PDF and attach filename/page metadata."""
    pdf_filename = os.path.basename(pdf_path)
    all_rows = []
    headers = None

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            table = page.extract_table()
            if not table:
                continue

            for row_idx, row in enumerate(table):
                # Clean empty strings / nulls
                cleaned_row = [cell.strip() if cell else "" for cell in row]

                # Identify header row from the first non-empty page table
                if headers is None:
                    if any(cleaned_row):
                        headers = [clean_col_name(c) for c in cleaned_row]
                        # Ensure headers are unique
                        seen = {}
                        uniq_headers = []
                        for h in headers:
                            if h in seen:
                                seen[h] += 1
                                uniq_headers.append(f"{h}_{seen[h]}")
                            else:
                                seen[h] = 0
                                uniq_headers.append(h)
                        headers = uniq_headers
                    continue

                # Skip header repetitions on subsequent pages
                if [clean_col_name(c) for c in cleaned_row] == headers:
                    continue

                # Skip completely empty rows
                if not any(cleaned_row):
                    continue

                # Append metadata column (PDF name and page number)
                source_info = f"{pdf_filename} - Page {page_idx}"
                row_data = cleaned_row[: len(headers)]

                # Pad row if shorter than headers
                if len(row_data) < len(headers):
                    row_data += [""] * (len(headers) - len(row_data))

                row_dict = dict(zip(headers, row_data))
                row_dict["SOURCE_LOCATION"] = source_info
                all_rows.append(row_dict)

    df = pd.DataFrame(all_rows)
    return df


def sync_and_alter_schema(
    conn: sqlite3.Connection, table_name: str, new_cols: list
):
    """Dynamically add new columns to the SQLite table if they do not exist."""
    cursor = conn.cursor()

    # Check if table exists
    cursor.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}';"
    )
    table_exists = cursor.fetchone()

    if not table_exists:
        # Create table with all current columns as TEXT
        cols_sql = ", ".join([f'"{col}" TEXT' for col in new_cols])
        cursor.execute(f'CREATE TABLE "{table_name}" ({cols_sql});')
    else:
        # Get existing columns
        cursor.execute(f'PRAGMA table_info("{table_name}");')
        existing_cols = {row[1] for row in cursor.fetchall()}

        # Add missing columns dynamically
        for col in new_cols:
            if col not in existing_cols:
                cursor.execute(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT;'
                )

    conn.commit()


def export_pdf_to_sqlite(
    pdf_path: str,
    db_path: str,
    table_name: str = "timetable",
    subject_code_col: str = "COURSE_NO",
):
    """Main function to parse PDF and insert non-duplicate rows into SQLite database."""
    # 1. Extract data from PDF
    df = extract_pdf_tables(pdf_path)

    if df.empty:
        print("No table data found in PDF.")
        return

    # Normalize subject code column if present (e.g. COURSE_NO or COM_COD)
    if subject_code_col not in df.columns:
        possible_matches = [
            c for c in df.columns if "COURSE" in c or "COD" in c
        ]
        if possible_matches:
            subject_code_col = possible_matches[0]

    # 2. Connect to SQLite and sync schema
    conn = sqlite3.connect(db_path)

    sync_and_alter_schema(conn, table_name, list(df.columns))

    # 3. Prevent Duplicate Entries
    cursor = conn.cursor()
    cursor.execute(
        f'SELECT DISTINCT "{subject_code_col}" FROM "{table_name}" WHERE "{subject_code_col}" IS NOT NULL AND "{subject_code_col}" != "";'
    )
    existing_codes = {row[0] for row in cursor.fetchall()}

    # Filter out rows that already exist in DB based on subject code
    if subject_code_col in df.columns:
        initial_count = len(df)
        df_to_insert = df[~df[subject_code_col].isin(existing_codes)]
        skipped = initial_count - len(df_to_insert)
        if skipped > 0:
            print(
                f"Skipped {skipped} duplicate rows matching existing subject codes."
            )
    else:
        df_to_insert = df

    # 4. Append new rows to SQLite database
    if not df_to_insert.empty:
        df_to_insert.to_sql(
            table_name, conn, if_exists="append", index=False
        )
        print(
            f"Successfully inserted {len(df_to_insert)} new rows into table '{table_name}'."
        )
    else:
        print("No new unique records to insert.")

    conn.close()


if __name__ == "__main__":
    PDF_FILE = "dataset/timetable_cropped.pdf"
    DB_FILE = "course.db"
    TABLE_NAME = "course_timetable"

    export_pdf_to_sqlite(
        pdf_path=PDF_FILE,
        db_path=DB_FILE,
        table_name=TABLE_NAME,
        subject_code_col="COURSE_NO",
    )
'''