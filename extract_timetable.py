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

headers_main = ["COM_CODE","course_code","course_title","credits","course_IC","midsem_details","compre_details","source_info"]
headers_classes = ["COM_CODE","course_code","classes_type","instructors","rooms","timings"]

def extract_pdf_tables(pdf_path: str) -> pd.DataFrame:
    """Extract all tables from multi-page PDF and attach filename/page metadata."""
    pdf_filename = os.path.basename(pdf_path)
    all_courses = []
    all_classes = []
    headers = None

    with pdfplumber.open(pdf_path) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            table = page.extract_table()
            if not table:
                continue

            for row_idx, row in enumerate(table):
                #print("outer_loop: ",row_idx)
                # Clean empty strings / nulls
                cleaned_row = [cell.strip() if cell else "" for cell in row]

                
                row_data_main = ["","","","","","","",""]
                row_data_classes = ["","","","","",""]

                if cleaned_row[1] == "COURSE NO.":
                    continue

                #print(cleaned_row[1])
                if cleaned_row[1] != ""  and cleaned_row[9] != "":# and cleaned_row[1] != "COURSE NO.": 
                    
                    # Append metadata column (PDF name and page number)
                    source_info = f"{pdf_filename} - Page {page_idx}"

                    row_data_main[:3] = cleaned_row[0:3]
                    row_data_main[3] = cleaned_row[7]
                    row_data_main[4] = cleaned_row[9]
                    row_data_main[5:7] = cleaned_row[12:]
                    row_data_main[7] = source_info

                    row_dict = dict(zip(headers_main, row_data_main))
                    all_courses.append(row_dict)

                    table_loop = table
                    i = row_idx
                    j = page_idx
                    course_code = cleaned_row[1]
                    Com_CODE = cleaned_row[0]
                    while (cleaned_row[1] == course_code or cleaned_row[1] == "") and j <= len(pdf.pages):
                        
                        #print("inner loop: ",i)
                        if(cleaned_row[8]!= ""):
                            
                            if i != row_idx:
                                classes_dict = dict(zip(headers_classes, row_data_classes))
                                all_classes.append(classes_dict)
                                #print(classes_dict)

                            row_data_classes[0] = Com_CODE
                            row_data_classes[1] = course_code
                            row_data_classes[2] = cleaned_row[8]
                            row_data_classes[3] = cleaned_row[9]
                            row_data_classes[4] = cleaned_row[10]
                            row_data_classes[5] = cleaned_row[11]
                        
                        else:
                            row_data_classes[3] = row_data_classes[3] +  " " + cleaned_row[9] 
                        
                        if i >= (len(table_loop)-1) and j< len(pdf.pages):
                            if(course_code == 'BIO U101'):
                                print('page before: ', j)
                            j = j+1
                            table_loop = pdf.pages[j].extract_table()
                            while not table_loop:
                                j = j+1
                                table_loop = pdf.pages[j].extract_table()
                            if(course_code == 'BIO U101'):
                                print('page after: ' ,j)
                            
                            i = 1
                        
                         
                        else:
                            i = i+1

                        if(i < len(table_loop)):
                            cleaned_row = [cell.strip() if cell else "" for cell in table_loop[i]]
                        else:
                            j = j+1
                            
                        
                        #print("inner loop page:",j)
                        


                    classes_dict = dict(zip(headers_classes, row_data_classes))
                    all_classes.append(classes_dict)

    df_classes = pd.DataFrame(all_classes)
    df_courses = pd.DataFrame(all_courses)
    return df_courses,df_classes


def init_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        COM_CODE TEXT UNIQUE,
        course_code TEXT ,
        course_title TEXT, 
        credits REAL,
        course_IC TEXT, 
        midsem_details TEXT,
        compre_details TEXT,
        -- source metadata
        source_info TEXT
        -- verification
    );
    CREATE TABLE IF NOT EXISTS classes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        course_code TEXT,
        classes_type TEXT, 
        instructors TEXT, 
        rooms TEXT, 
        timings TEXT
    );
    """)
    return conn

def save_to_database(conn, courses_df, classes_df):
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        
        # 1. Save courses DataFrame
        courses_df.to_sql('courses', conn, if_exists='append', index=False)

        # 2. Get auto-generated course IDs from SQLite
        id_map = pd.read_sql_query("SELECT id, COM_CODE FROM courses", conn)

        # 3. Map string course_code in classes_df to foreign key integer ID
        merged_classes = classes_df.merge(
            id_map, 
            on='COM_CODE', 
            how='inner'
        )

        # 4. Prepare exact columns expected by the 'classes' SQL table
        # Rename 'id' (from courses) to 'course_code' (foreign key column in classes table)
        final_classes = merged_classes[[
            'id','course_code','classes_type', 'instructors', 'rooms', 'timings'
        ]].rename(columns={'id': 'course_id'})

        # 5. Save classes DataFrame
        final_classes.to_sql('classes', conn, if_exists='append', index=False)

        conn.commit()
        print("Data successfully saved to database!")

    except Exception as e:
        conn.rollback()
        print(f"Failed to save data: {e}")



if __name__ == "__main__":
    PDF_FILE = "dataset/timetable_cropped.pdf"
    DB_FILE = "course.db"
    TABLE_NAME = "course_timetable"

    df_courses,df_classes = extract_pdf_tables(PDF_FILE)
    conn = init_db(DB_FILE)
    save_to_database(conn,df_courses,df_classes)
