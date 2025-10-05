import sqlite3

DB_PATH = "data/processed_emails.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS processed_emails (
        email_id TEXT PRIMARY KEY,
        sender TEXT,
        subject TEXT,
        date TIMESTAMP,
        processed_at TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def mark_email_processed(email_id: str, sender: str, subject: str, date: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR IGNORE INTO processed_emails (email_id, sender, subject, date, processed_at)
    VALUES (?, ?, ?, ?, datetime('now'))
    """, (email_id, sender, subject, date))
    conn.commit()
    conn.close()
