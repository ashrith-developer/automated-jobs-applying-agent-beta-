import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobflow.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create applications table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_title TEXT NOT NULL,
        company TEXT NOT NULL,
        location TEXT NOT NULL,
        link TEXT NOT NULL,
        date_applied TEXT NOT NULL,
        status TEXT NOT NULL, -- 'Applied', 'Draft', 'Failed', 'Applying'
        cover_letter TEXT,
        log_output TEXT,
        screenshot_path TEXT
    )
    """)
    
    # Create Q&A Cache table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS qa_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question TEXT UNIQUE NOT NULL,
        answer TEXT NOT NULL,
        date_added TEXT NOT NULL
    )
    """)
    
    conn.commit()
    conn.close()

def add_application(job_title: str, company: str, location: str, link: str, status: str = "Applied", cover_letter: str = "", log_output: str = "", screenshot_path: str = "") -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute("""
    INSERT INTO applications (job_title, company, location, link, date_applied, status, cover_letter, log_output, screenshot_path)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (job_title, company, location, link, date_now, status, cover_letter, log_output, screenshot_path))
    
    app_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return app_id

def update_application(app_id: int, status: str, log_output: str = None, screenshot_path: str = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if log_output and screenshot_path:
        cursor.execute("""
        UPDATE applications SET status = ?, log_output = ?, screenshot_path = ? WHERE id = ?
        """, (status, log_output, screenshot_path, app_id))
    elif log_output:
        cursor.execute("""
        UPDATE applications SET status = ?, log_output = ? WHERE id = ?
        """, (status, log_output, app_id))
    elif screenshot_path:
        cursor.execute("""
        UPDATE applications SET status = ?, screenshot_path = ? WHERE id = ?
        """, (status, screenshot_path, app_id))
    else:
        cursor.execute("""
        UPDATE applications SET status = ? WHERE id = ?
        """, (status, app_id))
        
    conn.commit()
    conn.close()

def get_all_applications():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM applications ORDER BY date_applied DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_stats():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM applications")
    total = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'Applied'")
    applied = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'Failed'")
    failed = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM applications WHERE status = 'Draft'")
    draft = cursor.fetchone()[0]
    
    conn.close()
    
    success_rate = round((applied / total) * 100) if total > 0 else 0
    
    return {
        "total": total,
        "applied": applied,
        "failed": failed,
        "draft": draft,
        "success_rate": success_rate
    }

def get_cached_answer(question: str) -> str:
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Try exact match first
    cursor.execute("SELECT answer FROM qa_cache WHERE question = ?", (question,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return row[0]
        
    # Try case-insensitive substring match
    cursor.execute("SELECT question, answer FROM qa_cache")
    rows = cursor.fetchall()
    conn.close()
    
    question_lower = question.lower()
    for row in rows:
        cached_q = row[0].lower()
        # If the cached question is a major part of the input question or vice versa
        if cached_q in question_lower or question_lower in cached_q:
            return row[1]
            
    return None

def save_qa_pair(question: str, answer: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute("""
        INSERT OR REPLACE INTO qa_cache (question, answer, date_added)
        VALUES (?, ?, ?)
        """, (question, answer, date_now))
        conn.commit()
    except Exception as e:
        print(f"Error saving QA to SQLite cache: {e}")
    finally:
        conn.close()

# Initialize DB on import
init_db()
