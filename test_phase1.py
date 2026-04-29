#!/usr/bin/env python3
"""
test_phase1.py — Verify Phase 1: Foundation is working correctly.

Tests:
  1. SQLite database creation and schema validation
  2. Insert and query a dummy job + application
  3. Google Sheets connection, header creation, and dummy row append

Usage:
  1. Fill in your .env file (copy from .env.example)
  2. Share your Google Sheet with the service account email in sheets_credentials.json
  3. Run: python test_phase1.py
"""

import sys
import os
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def test_sqlite():
    """Test SQLite database creation, insert, and query."""
    print("\n" + "=" * 60)
    print("TEST 1: SQLite Database")
    print("=" * 60)

    from tracker.database import Database

    db = Database()

    # 1. Initialise schema
    db.init_db()
    print("✅ Database initialised — tables created.")

    # 2. Verify tables exist
    conn = db.connect()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t["name"] for t in tables]
    print(f"   Tables found: {table_names}")

    assert "jobs" in table_names, "❌ 'jobs' table missing!"
    assert "applications" in table_names, "❌ 'applications' table missing!"
    print("✅ Both 'jobs' and 'applications' tables exist.")

    # 3. Verify columns in jobs table
    cols = conn.execute("PRAGMA table_info(jobs)").fetchall()
    col_names = [c["name"] for c in cols]
    expected_job_cols = [
        "id", "platform", "company", "role", "jd", "salary",
        "location", "remote", "team_size", "subsidies", "url",
        "posted_date", "seen_at",
    ]
    for c in expected_job_cols:
        assert c in col_names, f"❌ Column '{c}' missing from jobs table!"
    print(f"✅ Jobs table has all {len(expected_job_cols)} expected columns.")

    # 4. Verify columns in applications table
    cols = conn.execute("PRAGMA table_info(applications)").fetchall()
    col_names = [c["name"] for c in cols]
    expected_app_cols = [
        "id", "job_id", "score", "reasoning", "missing_keywords",
        "ats_keywords", "cover_letter", "resume_version", "status",
        "applied_at", "follow_up_sent", "follow_up_draft", "created_at",
    ]
    for c in expected_app_cols:
        assert c in col_names, f"❌ Column '{c}' missing from applications table!"
    print(f"✅ Applications table has all {len(expected_app_cols)} expected columns.")

    # 5. Insert a dummy job
    dummy_job = {
        "platform": "wellfound",
        "company": "TestCorp",
        "role": "Software Engineer",
        "jd": "We are looking for a skilled Python developer...",
        "salary": "₹15-25 LPA",
        "location": "Bangalore, India",
        "remote": "Hybrid",
        "team_size": "11-50",
        "subsidies": "Health insurance, ESOP",
        "url": f"https://wellfound.com/jobs/test-{datetime.utcnow().timestamp()}",
        "posted_date": datetime.utcnow().isoformat(),
    }
    job_id = db.insert_job(dummy_job)
    print(f"✅ Inserted dummy job with ID: {job_id}")

    # 6. Check duplicate detection
    assert db.job_exists(dummy_job["url"]), "❌ job_exists() returned False for known URL!"
    print("✅ Duplicate detection works (job_exists = True).")

    # 7. Insert a dummy application
    dummy_app = {
        "job_id": job_id,
        "score": 8,
        "reasoning": "Strong Python skills match. Missing cloud experience.",
        "missing_keywords": ["AWS", "Docker", "Kubernetes"],
        "ats_keywords": ["Python", "REST API", "PostgreSQL"],
        "status": "scored",
    }
    app_id = db.insert_application(dummy_app)
    print(f"✅ Inserted dummy application with ID: {app_id}")

    # 8. Verify unscored jobs returns empty (since we just scored it)
    unscored = db.get_unscored_jobs()
    print(f"✅ Unscored jobs count: {len(unscored)} (should be 0)")

    # 9. Get stats
    stats = db.get_stats()
    print(f"✅ Stats: {stats}")

    db.close()
    print("\n🎉 SQLite tests PASSED!\n")
    return job_id


def test_google_sheets():
    """Test Google Sheets connection and dummy row append."""
    print("\n" + "=" * 60)
    print("TEST 2: Google Sheets Connection")
    print("=" * 60)

    from config import SHEETS_CREDENTIALS_PATH, SHEET_ID, SHEET_WORKSHEET_NAME
    from tracker.sheets import init_sheet, append_row

    print(f"   Credentials: {SHEETS_CREDENTIALS_PATH}")
    print(f"   Sheet ID:    {SHEET_ID}")
    print(f"   Worksheet:   {SHEET_WORKSHEET_NAME}")

    # 1. Initialise sheet
    worksheet = init_sheet(SHEETS_CREDENTIALS_PATH, SHEET_ID, SHEET_WORKSHEET_NAME)
    print(f"✅ Connected to worksheet: '{worksheet.title}'")

    # 2. Verify headers
    headers = worksheet.row_values(1)
    print(f"   Headers: {headers[:5]}... ({len(headers)} total)")
    assert len(headers) >= 10, "❌ Headers seem incomplete!"
    print("✅ Header row verified.")

    # 3. Append a dummy row
    dummy_data = {
        "Date": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
        "Platform": "wellfound",
        "Company": "TestCorp (Phase 1 Test)",
        "Role": "Software Engineer",
        "Location": "Bangalore",
        "Remote": "Hybrid",
        "Salary": "₹15-25 LPA",
        "Team Size": "11-50",
        "Score": "8",
        "Reasoning": "Strong match — test entry",
        "Missing Keywords": "AWS, Docker",
        "ATS Keywords": "Python, REST API",
        "Resume Version": "N/A (test)",
        "Status": "test",
        "Applied At": "",
        "Follow-up Sent": "No",
        "URL": "https://wellfound.com/jobs/test-phase1",
        "JD Snippet": "We are looking for a skilled Python developer...",
    }
    row_num = append_row(worksheet, dummy_data)
    print(f"✅ Appended dummy row at row {row_num}")
    print("\n🎉 Google Sheets tests PASSED!")
    print("   → Go check your Google Sheet to confirm the row appeared.\n")


def main():
    print("=" * 60)
    print("   JobHunter — Phase 1 Verification")
    print("=" * 60)

    # Test 1: SQLite (always runs)
    test_sqlite()

    # Test 2: Google Sheets (requires .env with SHEET_ID)
    try:
        from config import SHEET_ID
        if SHEET_ID and SHEET_ID != "your_google_sheet_id_here":
            test_google_sheets()
        else:
            print("\n⚠️  Skipping Google Sheets test — SHEET_ID not set in .env")
            print("   Fill in your .env file and re-run to test Sheets integration.")
    except Exception as e:
        print(f"\n❌ Google Sheets test FAILED: {e}")
        print("   Make sure you've:")
        print("   1. Created a Google Sheet and copied its ID to .env")
        print("   2. Shared the Sheet with: jobhunter-sheets@subtle-analyzer-494715-h8.iam.gserviceaccount.com")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("   Phase 1 verification complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
