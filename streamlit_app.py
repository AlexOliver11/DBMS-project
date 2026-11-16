import streamlit as st
import mysql.connector
import pandas as pd
import os
import re
from datetime import datetime
from PyPDF2 import PdfReader
from sentence_transformers import SentenceTransformer, util
from dotenv import load_dotenv
load_dotenv()

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME", "dbmsproject")
def get_connection():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME
    )

def fetch_df(conn, query, params=None):
    cursor = conn.cursor()
    cursor.execute(query, params or ())
    rows = cursor.fetchall()
    cols = cursor.column_names
    cursor.close()
    return pd.DataFrame(rows, columns=cols)

@st.cache_resource
def load_ai_model():
    return SentenceTransformer("all-MiniLM-L6-v2")

ai_model = load_ai_model()

# Initialize Session State Variables
if "db_initialized" not in st.session_state: st.session_state.db_initialized = False
if "extracted_data" not in st.session_state: st.session_state.extracted_data = None
if "metrics" not in st.session_state: st.session_state.metrics = None
if "form_inputs" not in st.session_state: st.session_state.form_inputs = {}
if "action_msg" not in st.session_state: st.session_state.action_msg = None

# Concurrency Control Session State
if "cc_conn_a" not in st.session_state: st.session_state.cc_conn_a = None
if "cc_locked_id" not in st.session_state: st.session_state.cc_locked_id = None
if "cc_staged" not in st.session_state: st.session_state.cc_staged = False
if "cc_events" not in st.session_state: st.session_state.cc_events = []

def release_session_a_lock():
    """Safely rolls back and closes any active lock held by Session A."""
    if st.session_state.cc_conn_a is not None:
        try:
            st.session_state.cc_conn_a.rollback()
            st.session_state.cc_conn_a.close()
        except Exception:
            pass
    st.session_state.cc_conn_a = None
    st.session_state.cc_locked_id = None
    st.session_state.cc_staged = False

# ==========================================
# 2. SCHEMA, NORMALIZATION & PL/SQL INIT
# ==========================================
def init_db():
    temp_conn = mysql.connector.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD)
    temp_cursor = temp_conn.cursor()
    temp_cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
    temp_conn.close()

    conn = get_connection()
    cursor = conn.cursor()
    
    tables = [
        '''CREATE TABLE IF NOT EXISTS Candidate (
            CandidateID INT PRIMARY KEY AUTO_INCREMENT, FullName VARCHAR(100) NOT NULL,
            Email VARCHAR(100) NOT NULL UNIQUE, Phone VARCHAR(15) NOT NULL UNIQUE,
            DateOfBirth DATE NOT NULL, Gender VARCHAR(10) CHECK(Gender IN ('Male', 'Female', 'Other')),
            RegisteredDate DATE NOT NULL DEFAULT (CURDATE())
        )''',
        '''CREATE TABLE IF NOT EXISTS JobPosting (
            JobID INT PRIMARY KEY AUTO_INCREMENT, JobTitle VARCHAR(100) NOT NULL,
            Department VARCHAR(50) NOT NULL, ExperienceRequired INT NOT NULL CHECK (ExperienceRequired >= 0),
            JobDescription TEXT, PostedDate DATE NOT NULL,
            Status VARCHAR(20) NOT NULL DEFAULT 'Open' CHECK (Status IN ('Open', 'Closed', 'On Hold'))
        )''',
        '''CREATE TABLE IF NOT EXISTS Skill (
            SkillID INT PRIMARY KEY AUTO_INCREMENT, SkillName VARCHAR(50) NOT NULL UNIQUE,
            SkillCategory VARCHAR(50) NOT NULL, DemandLevel VARCHAR(20) CHECK (DemandLevel IN ('Low', 'Medium', 'High')),
            CreatedDate DATE NOT NULL DEFAULT (CURDATE())
        )''',
        '''CREATE TABLE IF NOT EXISTS JobSkill (
            JobSkillID INT PRIMARY KEY AUTO_INCREMENT, JobID INT NOT NULL, SkillID INT NOT NULL,
            FOREIGN KEY (JobID) REFERENCES JobPosting(JobID) ON DELETE CASCADE,
            FOREIGN KEY (SkillID) REFERENCES Skill(SkillID) ON DELETE CASCADE, UNIQUE(JobID, SkillID)
        )''',
        '''CREATE TABLE IF NOT EXISTS Resume (
            ResumeID INT PRIMARY KEY AUTO_INCREMENT, CandidateID INT NOT NULL,
            FilePath VARCHAR(255) NOT NULL, UploadDate DATE NOT NULL,
            TotalExperienceYears DECIMAL(4,1) NOT NULL CHECK (TotalExperienceYears >= 0),
            ParsedScore DECIMAL(5,2) CHECK (ParsedScore >= 0 AND ParsedScore <= 100),
            FOREIGN KEY (CandidateID) REFERENCES Candidate(CandidateID) ON DELETE CASCADE
        )''',
        '''CREATE TABLE IF NOT EXISTS CandidateSkill (
            CandidateSkillID INT PRIMARY KEY AUTO_INCREMENT, CandidateID INT NOT NULL, SkillID INT NOT NULL,
            ProficiencyLevel VARCHAR(20) NOT NULL CHECK (ProficiencyLevel IN ('Beginner', 'Intermediate', 'Advanced', 'Expert')),
            YearsOfExperience DECIMAL(3,1) NOT NULL CHECK (YearsOfExperience >= 0),
            FOREIGN KEY (CandidateID) REFERENCES Candidate(CandidateID) ON DELETE CASCADE,
            FOREIGN KEY (SkillID) REFERENCES Skill(SkillID) ON DELETE CASCADE, UNIQUE(CandidateID, SkillID)
        )''',
        '''CREATE TABLE IF NOT EXISTS ScreeningResult (
            ScreeningID INT PRIMARY KEY AUTO_INCREMENT, CandidateID INT NOT NULL, JobID INT NOT NULL,
            AIMatchScore DECIMAL(5,2) NOT NULL CHECK (AIMatchScore >= 0 AND AIMatchScore <= 100),
            ScreeningDate DATE NOT NULL, Status VARCHAR(25) NOT NULL DEFAULT 'Pending', Remarks VARCHAR(255),
            FOREIGN KEY (CandidateID) REFERENCES Candidate(CandidateID) ON DELETE CASCADE,
            FOREIGN KEY (JobID) REFERENCES JobPosting(JobID) ON DELETE CASCADE
        )''',
        '''CREATE TABLE IF NOT EXISTS ScreeningDetail (
            DetailID INT PRIMARY KEY AUTO_INCREMENT, ScreeningID INT NOT NULL,
            SkillScore DECIMAL(5,2), ProficiencyScore DECIMAL(5,2), ExperienceScore DECIMAL(5,2), SimilarityScore DECIMAL(5,2),
            FOREIGN KEY (ScreeningID) REFERENCES ScreeningResult(ScreeningID) ON DELETE CASCADE
        )'''
    ]
    for q in tables: cursor.execute(q)

    cursor.execute("DROP TRIGGER IF EXISTS trg_AutoStatus")
    cursor.execute('''
        CREATE TRIGGER trg_AutoStatus
        BEFORE INSERT ON ScreeningResult
        FOR EACH ROW
        SET NEW.Status = CASE
            WHEN NEW.AIMatchScore >= 80 THEN 'Shortlisted'
            WHEN NEW.AIMatchScore >= 60 THEN 'Pending'
            ELSE 'Rejected'
        END;
    ''')

    cursor.execute("DROP PROCEDURE IF EXISTS sp_GetTopCandidates")
    cursor.execute('''
        CREATE PROCEDURE sp_GetTopCandidates(IN p_JobID INT)
        SELECT c.CandidateID, c.FullName, c.Email, r.TotalExperienceYears, sr.AIMatchScore, sr.Status
        FROM Candidate c
        JOIN Resume r ON c.CandidateID = r.CandidateID
        JOIN ScreeningResult sr ON c.CandidateID = sr.CandidateID
        WHERE sr.JobID = p_JobID
        ORDER BY sr.AIMatchScore DESC;
    ''')
    
    cursor.execute("SELECT COUNT(*) FROM JobPosting")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("INSERT IGNORE INTO JobPosting (JobID, JobTitle, Department, ExperienceRequired, JobDescription, PostedDate, Status) VALUES (%s, %s, %s, %s, %s, CURDATE(), 'Open')", [
            (1, 'Data Scientist', 'AI', 3, 'Looking for a Data Scientist who builds REST services in Flask, handles ML pipelines, and knows Python and SQL.'),
            (2, 'Backend Developer', 'Engineering', 2, 'Seeking a Backend Developer with strong Java, Spring Boot, and SQL database skills.'),
            (3, 'Product Manager', 'Product', 5, 'Need an experienced PM well-versed in Agile methodologies, scrum, and product lifecycle management.'),
            (4, 'UX Designer', 'Design', 2, 'Looking for a creative UX Designer proficient in Figma, wireframing, and user research.'),
            (5, 'DevOps Engineer', 'Infrastructure', 4, 'Require a DevOps Engineer to manage AWS cloud infrastructure and deploy Kubernetes clusters.')
        ])
        cursor.executemany("INSERT IGNORE INTO Skill (SkillID, SkillName, SkillCategory, DemandLevel, CreatedDate) VALUES (%s, %s, %s, %s, CURDATE())", [
            (1, 'Python', 'Programming', 'High'), (2, 'SQL', 'Database', 'High'), (3, 'Flask', 'Backend', 'Medium'),
            (4, 'Java', 'Programming', 'High'), (5, 'Agile', 'Methodology', 'Medium'), (6, 'Figma', 'Design', 'High'),
            (7, 'AWS', 'Cloud', 'High'), (8, 'Kubernetes', 'DevOps', 'High'), (9, 'Machine Learning', 'AI', 'High')
        ])
        cursor.executemany("INSERT IGNORE INTO JobSkill (JobID, SkillID) VALUES (%s, %s)", [
            (1, 1), (1, 2), (1, 3), (1, 9),
            (2, 2), (2, 4),
            (3, 5),
            (4, 6),
            (5, 7), (5, 8)
        ])
        cursor.executemany("INSERT IGNORE INTO Candidate (CandidateID, FullName, Email, Phone, DateOfBirth, Gender, RegisteredDate) VALUES (%s, %s, %s, %s, %s, %s, CURDATE())", [
            (1, 'Alice Smith', 'alice@test.com', '111-222-3333', '1995-05-15', 'Female'),
            (2, 'Bob Johnson', 'bob@test.com', '444-555-6666', '1992-10-22', 'Male')
        ])
        cursor.executemany("INSERT IGNORE INTO Resume (ResumeID, CandidateID, FilePath, UploadDate, TotalExperienceYears, ParsedScore) VALUES (%s, %s, %s, CURDATE(), %s, %s)", [
            (1, 1, '/uploads/alice.pdf', 4.0, 95.0),
            (2, 2, '/uploads/bob.pdf', 2.5, 90.0)
        ])
        cursor.executemany("INSERT IGNORE INTO CandidateSkill (CandidateSkillID, CandidateID, SkillID, ProficiencyLevel, YearsOfExperience) VALUES (%s, %s, %s, %s, %s)", [
            (1, 1, 1, 'Expert', 4.0), (2, 1, 2, 'Advanced', 4.0), (3, 1, 9, 'Advanced', 3.0),
            (4, 2, 4, 'Intermediate', 2.0), (5, 2, 2, 'Intermediate', 2.5)
        ])
        cursor.executemany("INSERT IGNORE INTO ScreeningResult (ScreeningID, CandidateID, JobID, AIMatchScore, ScreeningDate, Status, Remarks) VALUES (%s, %s, %s, %s, CURDATE(), %s, %s)", [
            (1, 1, 1, 88.50, 'Shortlisted', 'Strong ML background'),
            (2, 2, 2, 72.00, 'Pending', 'Good Java, low experience')
        ])
        cursor.executemany("INSERT IGNORE INTO ScreeningDetail (DetailID, ScreeningID, SkillScore, ProficiencyScore, ExperienceScore, SimilarityScore) VALUES (%s, %s, %s, %s, %s, %s)", [
            (1, 1, 90.0, 85.0, 100.0, 79.0),
            (2, 2, 75.0, 50.0, 100.0, 63.0)
        ])
    
    conn.commit()
    conn.close()

# ==========================================
# 3. AI EXTRACTION & SCORING
# ==========================================
def extract_text_from_pdf(file):
    text = ""
    for page in PdfReader(file).pages:
        if extracted := page.extract_text(): text += extracted + "\n"
    return text

def is_valid_resume(text):
    if len(text.strip()) < 50: return False
    keywords = ['education', 'experience', 'skills', 'work', 'employment', 'projects', 'summary', 'university', 'degree']
    return sum(1 for kw in keywords if kw in text.lower()) >= 2

def parse_resume_data(text, all_db_skills):
    parsed_score = 40.0 
    email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    email = email_match.group(0) if email_match else f"unknown_{int(datetime.now().timestamp())}@test.com"
    if email_match: parsed_score += 20.0
    
    phone_match = re.search(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', text)
    phone = phone_match.group(0) if phone_match else f"000-000-{str(datetime.now().timestamp())[-4:]}"
    if phone_match: parsed_score += 20.0
    
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    name = lines[0][:50] if lines else "Unknown Candidate"
    
    exp_match = re.search(r'(\d+)\+?\s*years?', text, re.IGNORECASE)
    experience = float(exp_match.group(1)) if exp_match else 0.0
    if exp_match: parsed_score += 20.0
        
    cand_skills = {s_name: {"id": s_id, "prof": "Intermediate"} for s_id, s_name in all_db_skills if s_name.lower() in text.lower()}
            
    return {"name": name, "email": email, "phone": phone, "experience": experience, "skills": cand_skills, "raw_text": text, "parsed_score": min(100.0, parsed_score)}

def compute_ai_score(cand_data, job_desc, req_skills_list, req_exp):
    PROF_MAP = {"Beginner": 25, "Intermediate": 50, "Advanced": 75, "Expert": 100}
    cand_skill_names = list(cand_data['skills'].keys())
    
    if req_skills_list:
        matched = [s for s in req_skills_list if s in cand_skill_names]
        skill_score = (len(matched) / len(req_skills_list)) * 100
        prof_score = sum(PROF_MAP[cand_data['skills'][s]['prof']] for s in matched) / max(len(matched), 1)
    else:
        skill_score, prof_score = 100.0, 100.0

    exp_score = min(cand_data['experience'] / req_exp, 1.0) * 100 if req_exp > 0 else 100.0
    
    sim_score = 0.0
    if job_desc:
        sim_score = float(util.cos_sim(ai_model.encode(cand_data['raw_text']), ai_model.encode(job_desc))) * 100
        sim_score = max(0, min(100, sim_score)) 
        
    total_score = (0.35 * skill_score) + (0.15 * prof_score) + (0.25 * exp_score) + (0.25 * sim_score)
        
    return {
        "Total": round(total_score, 2), "SkillScore": round(skill_score, 2), 
        "ProfScore": round(prof_score, 2), "ExpScore": round(exp_score, 2), "SimScore": round(sim_score, 2),
        "Status": "Awaiting DB Trigger" 
    }

# ==========================================
# 4. STREAMLIT GUI
# ==========================================
st.set_page_config(page_title="AI Resume Screener", layout="wide")
st.title("AI Resume Screening System")

# Run init_db() ONLY ONCE so DDL statements never block open locks on reruns
if not st.session_state.db_initialized:
    try:
        init_db()
        st.session_state.db_initialized = True
    except Exception:
        pass

choice = st.sidebar.selectbox("Navigation", [
    "Upload & Extract Resume", 
    "Normalization Logic", 
    "Concurrency Control",
    "Reports (PL/SQL)", 
    "Manage Database (CRUD)"
])

# Auto-cleanup if user leaves Concurrency Control while holding an open lock
if choice != "Concurrency Control" and st.session_state.cc_conn_a is not None:
    release_session_a_lock()

if choice == "Upload & Extract Resume":
    st.header("Upload Resume for Processing")
    conn = get_connection()
    jobs = fetch_df(conn, "SELECT JobID, JobTitle FROM JobPosting WHERE Status = 'Open'")
    
    if jobs.empty: st.warning("No open jobs available.")
    else:
        job_options = {row['JobTitle']: row['JobID'] for _, row in jobs.iterrows()}
        selected_job_title = st.selectbox("Select Target Job", list(job_options.keys()))
        selected_job_id = job_options[selected_job_title]
        
        uploaded_file = st.file_uploader("Upload Candidate Resume (PDF)", type=["pdf"])
        dob = st.date_input("Candidate Date of Birth")
        gender = st.selectbox("Gender", ["Male", "Female", "Other"])

        if st.button("Extract & Run AI Scoring") and uploaded_file:
            text = extract_text_from_pdf(uploaded_file)
            if not is_valid_resume(text):
                st.error("❌ Invalid Document: This PDF does not appear to be a resume.")
            else:
                if not os.path.exists("uploads"): os.makedirs("uploads")
                file_path = os.path.join("uploads", uploaded_file.name)
                with open(file_path, "wb") as f: f.write(uploaded_file.getbuffer())
                
                cursor = conn.cursor()
                cursor.execute("SELECT SkillID, SkillName FROM Skill")
                all_db_skills = cursor.fetchall()
                
                cursor.execute("SELECT s.SkillName FROM JobSkill js JOIN Skill s ON js.SkillID = s.SkillID WHERE js.JobID = %s", (selected_job_id,))
                req_skills = [row[0] for row in cursor.fetchall()]
                cursor.execute("SELECT ExperienceRequired, JobDescription FROM JobPosting WHERE JobID = %s", (selected_job_id,))
                req_exp, job_desc = cursor.fetchone()
                
                parsed_data = parse_resume_data(text, all_db_skills)
                st.session_state.extracted_data = parsed_data
                st.session_state.metrics = compute_ai_score(parsed_data, job_desc, req_skills, req_exp)
                st.session_state.form_inputs = {'job_id': selected_job_id, 'dob': dob, 'gender': gender, 'file_path': file_path}

        if st.session_state.extracted_data is not None:
            st.divider()
            st.json({k: v for k, v in st.session_state.extracted_data.items() if k not in ['raw_text', 'parsed_score']})
            
            metrics = st.session_state.metrics
            cols = st.columns(5)
            cols[0].metric("Total AI Score", f"{metrics['Total']}/100")
            cols[1].metric("Skill Match", f"{metrics['SkillScore']}/100")
            cols[2].metric("Proficiency", f"{metrics['ProfScore']}/100")
            cols[3].metric("Experience Match", f"{metrics['ExpScore']}/100")
            cols[4].metric("Semantic Similarity", f"{metrics['SimScore']}/100")
            
            st.info(f"**Status:** {metrics['Status']} (MySQL Trigger will calculate final Shortlist/Reject status)")
            
            if st.button("Confirm & Insert into Database", type="primary"):
                try:
                    conn.autocommit = False
                    cursor = conn.cursor()
                    cursor.execute("START TRANSACTION")
                    inputs, data = st.session_state.form_inputs, st.session_state.extracted_data
                    
                    cursor.execute('''INSERT INTO Candidate (FullName, Email, Phone, DateOfBirth, Gender, RegisteredDate) VALUES (%s, %s, %s, %s, %s, CURDATE())''', 
                                   (data['name'], data['email'], data['phone'], inputs['dob'], inputs['gender']))
                    cand_id = cursor.lastrowid
                    
                    cursor.execute('''INSERT INTO Resume (CandidateID, FilePath, UploadDate, TotalExperienceYears, ParsedScore) VALUES (%s, %s, CURDATE(), %s, %s)''', 
                                   (cand_id, inputs['file_path'], data['experience'], data['parsed_score']))
                    
                    for skill_name, skill_info in data['skills'].items():
                        cursor.execute('''INSERT IGNORE INTO CandidateSkill (CandidateID, SkillID, ProficiencyLevel, YearsOfExperience) VALUES (%s, %s, %s, %s)''', 
                                       (cand_id, skill_info['id'], skill_info['prof'], data['experience']))
                    
                    cursor.execute('''INSERT INTO ScreeningResult (CandidateID, JobID, AIMatchScore, ScreeningDate, Status, Remarks) VALUES (%s, %s, %s, CURDATE(), 'Pending', 'AI Scored')''', 
                                   (cand_id, inputs['job_id'], metrics['Total']))
                    
                    cursor.execute('''INSERT INTO ScreeningDetail (ScreeningID, SkillScore, ProficiencyScore, ExperienceScore, SimilarityScore) VALUES (%s, %s, %s, %s, %s)''', 
                                   (cursor.lastrowid, metrics['SkillScore'], metrics['ProfScore'], metrics['ExpScore'], metrics['SimScore']))
                    
                    conn.commit()
                    st.success("✅ Atomic transaction committed across all 5 tables! The MySQL trigger categorized this candidate.")
                    st.session_state.extracted_data, st.session_state.metrics, st.session_state.form_inputs = None, None, {}
                except mysql.connector.Error as e:
                    conn.rollback()
                    st.error(f"Transaction Rolled Back due to Database Error: {e}")
    conn.close()

elif choice == "Normalization Logic":
    st.header("Database Normalization Breakdown (1NF – 5NF)")
    st.write("Select a table and a normal form to see how the schema eliminates anomalies, multi-valued dependencies, and join dependencies.")
    
    norm_rules = {
        "Candidate": {
            "1NF": "All attributes (`FullName`, `Email`, `Phone`, `DateOfBirth`) contain atomic, scalar values. Repeating groups like multiple resumes or skills are excluded.",
            "2NF": "The primary key `CandidateID` is a single attribute, so partial dependencies on a composite key are impossible.",
            "3NF": "Every non-key attribute depends solely on `CandidateID`. No transitive dependencies exist between demographic or contact fields.",
            "4NF": "Eliminates Multi-Valued Dependencies (MVDs). A candidate's uploaded **Resumes** and **Skills** are independent 1:N / M:N facts; keeping them in separate tables (`Resume` and `CandidateSkill`) avoids Cartesian product redundancy.",
            "5NF": "Project-Join Normal Form (PJNF): Keyed by a single atomic identifier (`CandidateID`) with no cyclic ternary relationships, so it cannot be losslessly decomposed into smaller projections."
        },
        "CandidateSkill": {
            "1NF": "Each row holds one atomic `(CandidateID, SkillID)` pairing with single scalar values for `ProficiencyLevel` and `YearsOfExperience`.",
            "2NF": "The logical composite key is `(CandidateID, SkillID)`. `ProficiencyLevel` and `YearsOfExperience` depend on the **entire** composite key, not on `CandidateID` or `SkillID` alone.",
            "3NF": "Neither `ProficiencyLevel` nor `YearsOfExperience` transitively determines any other non-key column.",
            "4NF": "No independent multi-valued facts exist in this table. Each row captures one specific skill evaluation for one candidate without bundling unrelated multi-valued attributes.",
            "5NF": "Models a direct binary relationship (`Candidate` ↔ `Skill`) with pairing-specific metrics. Since it is binary rather than a decomposable ternary relation, no non-trivial join dependencies exist."
        },
        "JobPosting": {
            "1NF": "Atomic scalar values only. Required skills are not stored as comma-separated arrays inside `JobPosting`.",
            "2NF": "Single-column Primary Key (`JobID`) guarantees zero partial dependencies.",
            "3NF": "Attributes (`JobTitle`, `Department`, `ExperienceRequired`, `Status`) depend directly on `JobID` with no transitive dependencies.",
            "4NF": "Separates independent multi-valued relationships: a job's required skills (`JobSkill`) and its candidate evaluations (`ScreeningResult`) live in distinct tables, preventing MVD duplication.",
            "5NF": "All columns describe intrinsic properties of a single job posting (`JobID`). No join dependency allows decomposing this table into smaller tables without losing data."
        },
        "JobSkill": {
            "1NF": "Stores single atomic foreign key IDs (`JobID`, `SkillID`) per row with no repeating groups.",
            "2NF": "All non-surrogate attributes belong to the unique composite key `(JobID, SkillID)`, eliminating partial dependency.",
            "3NF": "Contains no non-key descriptive attributes (skill categories or job titles remain in `Skill` and `JobPosting`), eliminating transitive dependencies.",
            "4NF": "A pure binary relationship table `(JobID, SkillID)` in 3NF/BCNF has no independent multi-valued attributes, satisfying 4NF by definition.",
            "5NF": "An all-key binary relation `(JobID, SkillID)` cannot be decomposed into smaller projections, automatically satisfying Project-Join Normal Form (5NF)."
        },
        "Skill": {
            "1NF": "Each skill is stored once with atomic attributes (`SkillName`, `SkillCategory`, `DemandLevel`).",
            "2NF": "Single-column Primary Key (`SkillID`) prevents partial dependencies.",
            "3NF": "`SkillCategory` and `DemandLevel` depend strictly on `SkillID` with no transitive dependencies.",
            "4NF": "Single-entity lookup table with single-valued attributes; no multi-valued dependencies exist.",
            "5NF": "Atomic lookup entity keyed by `SkillID` (and unique `SkillName`); cannot be decomposed further without breaking entity integrity."
        },
        "Resume": {
            "1NF": "Each row represents a single uploaded PDF (`FilePath`) with atomic numeric metrics (`TotalExperienceYears`, `ParsedScore`).",
            "2NF": "Single-column Primary Key (`ResumeID`) eliminates partial dependencies.",
            "3NF": "File metadata and parsed scores depend strictly on `ResumeID`. Candidate details are referenced via `CandidateID` rather than duplicated.",
            "4NF": "Separates resume file versions (`1:N` with `Candidate`) from candidate skills (`CandidateSkill`), preventing multi-valued dependency anomalies when a candidate uploads multiple CVs.",
            "5NF": "Keyed by `ResumeID` with a single foreign key (`CandidateID`); no cyclic or ternary join dependencies exist."
        },
        "ScreeningResult": {
            "1NF": "Stores atomic values (`AIMatchScore`, `ScreeningDate`, `Status`, `Remarks`) for a single screening run.",
            "2NF": "Single-column Primary Key (`ScreeningID`) prevents partial dependencies.",
            "3NF": "Candidate and job names are not duplicated here; only `CandidateID` and `JobID` foreign keys are stored, preventing update anomalies.",
            "4NF": "Captures a single evaluation event (`ScreeningID`) without embedding multi-valued skill lists or resume histories.",
            "5NF": "Although it references both `CandidateID` and `JobID`, it stores event-specific computed outcomes (`AIMatchScore`, `Status`) that cannot be reconstructed by joining separate binary tables."
        },
        "ScreeningDetail": {
            "1NF": "Each sub-score (`SkillScore`, `ProficiencyScore`, `ExperienceScore`, `SimilarityScore`) is stored in its own atomic numeric column.",
            "2NF": "Single-column Primary Key (`DetailID`) eliminates partial dependencies.",
            "3NF": "All four component scores depend directly on `DetailID` / `ScreeningID` with no transitive dependencies between scores.",
            "4NF": "Contains single-valued audit metrics for one `ScreeningID` with no independent multi-valued attributes.",
            "5NF": "Every metric belongs exclusively to a single `ScreeningID` evaluation and cannot be split across independent projections."
        }
    }
    
    col1, col2 = st.columns(2)
    with col1:
        sel_table = st.selectbox("1. Select a Table to Analyze", list(norm_rules.keys()))
    with col2:
        sel_nf = st.selectbox("2. Select Normal Form Rule", ["1NF", "2NF", "3NF", "4NF", "5NF"])
        
    st.divider()
    
    try:
        conn = get_connection()
        schema_df = fetch_df(conn, f"DESCRIBE {sel_table}")
        data_df = fetch_df(conn, f"SELECT * FROM {sel_table} LIMIT 3")
        conn.close()
        
        st.subheader(f"How `{sel_table}` satisfies {sel_nf}")
        st.info(norm_rules[sel_table][sel_nf])
        
        st.write("**Live Table Schema (Proof of Structure):**")
        st.dataframe(schema_df[['Field', 'Type', 'Key']], use_container_width=True)
        
        st.write(f"**Sample Data in `{sel_table}` (Proof of Atomicity):**")
        if data_df.empty:
            st.warning("Table is currently empty.")
        else:
            st.dataframe(data_df, use_container_width=True)
            
    except Exception as e:
        st.error(f"Error fetching data: {e}")

elif choice == "Concurrency Control":
    st.header("Concurrency Control: Exclusive Locks (`X-Lock`) & Rollbacks")
    st.write("Test real InnoDB **Exclusive Row-Level Locking (`SELECT ... FOR UPDATE`)**, **Uncommitted vs. Committed Isolation**, and **Transaction Rollbacks** without freezing.")

    try:
        conn = get_connection()
        iso_df = fetch_df(conn, "SELECT @@transaction_isolation AS IsolationLevel")
        screen_df = fetch_df(conn, """
            SELECT sr.ScreeningID, c.FullName AS Candidate, j.JobTitle AS Role, sr.AIMatchScore, sr.Status, sr.Remarks
            FROM ScreeningResult sr
            JOIN Candidate c ON sr.CandidateID = c.CandidateID
            JOIN JobPosting j ON sr.JobID = j.JobID
            ORDER BY sr.ScreeningID
        """)
        conn.close()
    except Exception as e:
        st.error(f"Database connection error: {e}")
        st.stop()

    is_locked = st.session_state.cc_conn_a is not None
    locked_id = st.session_state.cc_locked_id

    # Top Status Metrics
    m1, m2, m3 = st.columns(3)
    m1.metric("MySQL Isolation Level", iso_df.iloc[0]["IsolationLevel"])
    m2.metric("Lock Mode", "Exclusive Row Lock (X-Lock)")
    m3.metric("Lock Status", f"🔒 LOCKED (ID={locked_id})" if is_locked else "🟢 UNLOCKED")

    st.divider()

    if screen_df.empty:
        st.warning("No rows in `ScreeningResult`. Upload a resume first to test concurrency control.")
    else:
        row_options = {
            f"ScreeningID {r['ScreeningID']} — {r['Candidate']} ({r['Role']}) [{r['Status']}]": int(r['ScreeningID'])
            for _, r in screen_df.iterrows()
        }
        selected_label = st.selectbox(
            "Select Target Record in `ScreeningResult`:",
            list(row_options.keys()),
            disabled=is_locked
        )
        target_id = locked_id if is_locked else row_options[selected_label]

        # Split-Screen Workspace Cards
        col_a, col_b = st.columns(2)

        # ==========================================
        # SESSION A: EXCLUSIVE LOCK HOLDER
        # ==========================================
        with col_a:
            with st.container(border=True):
                if is_locked:
                    st.markdown(f"### 🧑‍💼 Session A — `🔒 HOLDING X-LOCK (ID={target_id})`")
                else:
                    st.markdown("### 🧑‍💼 Session A — `🟢 IDLE`")

                new_status_a = st.selectbox("Session A Status", ["Shortlisted", "Interview Scheduled", "Pending", "Rejected"], key="status_a")
                new_remark_a = st.text_input("Session A Remarks", value="Approved by Recruiter A", key="remark_a")

                if not is_locked:
                    if st.button("🔒 Acquire Exclusive Lock (`FOR UPDATE`)", type="primary", use_container_width=True):
                        try:
                            conn_a = get_connection()
                            conn_a.autocommit = False
                            cur_a = conn_a.cursor()
                            cur_a.execute("SET SESSION innodb_lock_wait_timeout = 1;")
                            cur_a.execute("START TRANSACTION;")
                            cur_a.execute("SELECT ScreeningID, Status, Remarks FROM ScreeningResult WHERE ScreeningID = %s FOR UPDATE;", (target_id,))
                            cur_a.fetchone()
                            cur_a.close()

                            st.session_state.cc_conn_a = conn_a
                            st.session_state.cc_locked_id = target_id
                            st.session_state.cc_staged = False
                            st.session_state.cc_events.insert(0, {
                                "Time": datetime.now().strftime("%H:%M:%S"),
                                "Session": "Session A",
                                "Action": f"START TRANSACTION; SELECT ... WHERE ScreeningID={target_id} FOR UPDATE",
                                "Lock": "🔒 X-Lock Acquired",
                                "Result": "Row locked exclusively; other writers blocked"
                            })
                            st.rerun()
                        except mysql.connector.Error as e:
                            st.error(f"Lock Error: {e}")
                else:
                    st.info("Session A holds an Exclusive Lock (`X-Lock`). You can stage an uncommitted update, then choose **COMMIT** or **ROLLBACK**.")
                    if st.button("✏️ Stage Uncommitted `UPDATE` in Session A", use_container_width=True):
                        try:
                            cur_a = st.session_state.cc_conn_a.cursor()
                            cur_a.execute(
                                "UPDATE ScreeningResult SET Status = %s, Remarks = %s WHERE ScreeningID = %s;",
                                (new_status_a, new_remark_a, target_id)
                            )
                            cur_a.close()
                            st.session_state.cc_staged = True
                            st.session_state.cc_events.insert(0, {
                                "Time": datetime.now().strftime("%H:%M:%S"),
                                "Session": "Session A",
                                "Action": f"UPDATE ScreeningResult SET Status='{new_status_a}' (UNCOMMITTED)",
                                "Lock": "🔒 X-Lock Held",
                                "Result": "Modified inside Session A only; invisible to public DB"
                            })
                            st.rerun()
                        except mysql.connector.Error as e:
                            st.error(f"Stage Error: {e}")

                    b1, b2 = st.columns(2)
                    with b1:
                        if st.button("✅ `COMMIT` & Unlock", type="primary", use_container_width=True):
                            try:
                                conn_a = st.session_state.cc_conn_a
                                if not st.session_state.cc_staged:
                                    cur_a = conn_a.cursor()
                                    cur_a.execute(
                                        "UPDATE ScreeningResult SET Status = %s, Remarks = %s WHERE ScreeningID = %s;",
                                        (new_status_a, new_remark_a, target_id)
                                    )
                                    cur_a.close()
                                conn_a.commit()
                                conn_a.close()
                                st.session_state.cc_conn_a = None
                                st.session_state.cc_locked_id = None
                                st.session_state.cc_staged = False
                                st.session_state.cc_events.insert(0, {
                                    "Time": datetime.now().strftime("%H:%M:%S"),
                                    "Session": "Session A",
                                    "Action": "COMMIT;",
                                    "Lock": "🔓 X-Lock Released",
                                    "Result": f"Saved Status='{new_status_a}' permanently"
                                })
                                st.rerun()
                            except mysql.connector.Error as e:
                                st.error(f"Commit Error: {e}")
                    with b2:
                        if st.button("↩️ `ROLLBACK` & Unlock", use_container_width=True):
                            try:
                                conn_a = st.session_state.cc_conn_a
                                conn_a.rollback()
                                conn_a.close()
                                st.session_state.cc_conn_a = None
                                st.session_state.cc_locked_id = None
                                st.session_state.cc_staged = False
                                st.session_state.cc_events.insert(0, {
                                    "Time": datetime.now().strftime("%H:%M:%S"),
                                    "Session": "Session A",
                                    "Action": "ROLLBACK;",
                                    "Lock": "🔓 X-Lock Released",
                                    "Result": "All uncommitted changes undone; original row restored"
                                })
                                st.rerun()
                            except mysql.connector.Error as e:
                                st.error(f"Rollback Error: {e}")

        # ==========================================
        # SESSION B: CONCURRENT WRITER
        # ==========================================
        with col_b:
            with st.container(border=True):
                if is_locked:
                    st.markdown(f"### 👩‍💼 Session B — `🛑 BLOCKED BY SESSION A (ID={target_id})`")
                else:
                    st.markdown("### 👩‍💼 Session B — `🟢 READY TO WRITE`")

                new_status_b = st.selectbox("Session B Status", ["Rejected", "Shortlisted", "Pending", "Interview Scheduled"], key="status_b")
                new_remark_b = st.text_input("Session B Remarks", value="Overwritten by Recruiter B", key="remark_b")

                if st.button("⚡ Attempt Concurrent `UPDATE` (Session B)", use_container_width=True):
                    conn_b = None
                    try:
                        conn_b = get_connection()
                        conn_b.autocommit = False
                        cur_b = conn_b.cursor()
                        # Fail fast (1s) so Streamlit never hangs while demonstrating lock contention
                        cur_b.execute("SET SESSION innodb_lock_wait_timeout = 1;")
                        cur_b.execute("START TRANSACTION;")
                        cur_b.execute(
                            "UPDATE ScreeningResult SET Status = %s, Remarks = %s WHERE ScreeningID = %s;",
                            (new_status_b, new_remark_b, target_id)
                        )
                        conn_b.commit()
                        cur_b.close()
                        conn_b.close()
                        st.session_state.cc_events.insert(0, {
                            "Time": datetime.now().strftime("%H:%M:%S"),
                            "Session": "Session B",
                            "Action": f"UPDATE ScreeningResult SET Status='{new_status_b}'; COMMIT;",
                            "Lock": "✅ Acquired & Released",
                            "Result": "No active lock held; Session B update succeeded"
                        })
                        st.rerun()
                    except mysql.connector.Error as e:
                        if conn_b:
                            conn_b.rollback()
                            conn_b.close()
                        st.session_state.cc_events.insert(0, {
                            "Time": datetime.now().strftime("%H:%M:%S"),
                            "Session": "Session B",
                            "Action": f"UPDATE ScreeningResult WHERE ScreeningID={target_id}",
                            "Lock": "🛑 Blocked by X-Lock",
                            "Result": f"Lost Update Prevented! ({e.msg})"
                        })
                        st.rerun()

                if is_locked:
                    st.warning("Click **Attempt Concurrent UPDATE** above while Session A holds the lock to see InnoDB block Session B and roll it back.")

        # ==========================================
        # ISOLATION PROOF: UNCOMMITTED VS COMMITTED VIEW
        # ==========================================
        if is_locked and st.session_state.cc_staged:
            st.subheader("🔍 Isolation Level Proof: Session A (Uncommitted) vs. Public DB (Committed)")
            iso_col1, iso_col2 = st.columns(2)
            with iso_col1:
                st.caption("**Inside Session A's Transaction (Uncommitted Dirty State):**")
                st.dataframe(fetch_df(st.session_state.cc_conn_a, "SELECT ScreeningID, Status, Remarks FROM ScreeningResult WHERE ScreeningID = %s", (target_id,)), use_container_width=True)
            with iso_col2:
                st.caption("**External Connection View (`REPEATABLE READ` — Old Committed State):**")
                pub_conn = get_connection()
                st.dataframe(fetch_df(pub_conn, "SELECT ScreeningID, Status, Remarks FROM ScreeningResult WHERE ScreeningID = %s", (target_id,)), use_container_width=True)
                pub_conn.close()

        # Live Transaction Timeline Table
        if st.session_state.cc_events:
            st.subheader("📜 Transaction & Lock Execution Timeline")
            st.dataframe(pd.DataFrame(st.session_state.cc_events), use_container_width=True)

        # Current Committed Database State
        st.subheader("📊 Current Committed `ScreeningResult` Table")
        conn = get_connection()
        st.dataframe(fetch_df(conn, """
            SELECT sr.ScreeningID, c.FullName AS Candidate, j.JobTitle AS Role, sr.AIMatchScore, sr.Status, sr.Remarks
            FROM ScreeningResult sr
            JOIN Candidate c ON sr.CandidateID = c.CandidateID
            JOIN JobPosting j ON sr.JobID = j.JobID
            ORDER BY sr.ScreeningID
        """), use_container_width=True)
        conn.close()

    st.divider()

    # ==========================================
    # AUTOMATIC CONSTRAINT ROLLBACK TEST
    # ==========================================
    st.subheader("🧪 Automatic ACID `ROLLBACK` on Constraint Failure")
    st.write("Executes a 2-step transaction: Step 1 updates `Remarks` validly, Step 2 sets `AIMatchScore = 150.00` (violating `CHECK 0–100`). MySQL rolls back both steps automatically.")
    if not screen_df.empty and st.button("Run Failing Multi-Step Transaction (`ROLLBACK`)"):
        conn_tx = None
        try:
            conn_tx = get_connection()
            conn_tx.autocommit = False
            cur_tx = conn_tx.cursor()
            cur_tx.execute("START TRANSACTION;")
            cur_tx.execute("UPDATE ScreeningResult SET Remarks = 'SHOULD_BE_ROLLED_BACK' WHERE ScreeningID = %s;", (target_id,))
            cur_tx.execute("UPDATE ScreeningResult SET AIMatchScore = 150.00 WHERE ScreeningID = %s;", (target_id,))
            conn_tx.commit()
        except mysql.connector.Error as e:
            if conn_tx:
                conn_tx.rollback()
                conn_tx.close()
            st.success(f"✅ **Automatic `ROLLBACK` Executed!** Step 2 failed (`{e.msg}`), so Step 1 (`SHOULD_BE_ROLLED_BACK`) was completely undone.")

elif choice == "Reports (PL/SQL)":
    st.header("Job Match Reports (Stored Procedure)")
    st.write("This report dynamically invokes the `sp_GetTopCandidates` Stored Procedure inside MySQL.")
    
    conn = get_connection()
    jobs = fetch_df(conn, "SELECT JobID, JobTitle FROM JobPosting")
    if not jobs.empty:
        job_options = {row['JobTitle']: row['JobID'] for _, row in jobs.iterrows()}
        selected_job_id = job_options[st.selectbox("Select Job Role to generate report:", list(job_options.keys()))]
        
        if st.button("Run Stored Procedure"):
            cursor = conn.cursor()
            cursor.callproc('sp_GetTopCandidates', [selected_job_id])
            for result in cursor.stored_results():
                df = pd.DataFrame(result.fetchall(), columns=result.column_names)
                if df.empty: st.info("No candidates screened for this role yet.")
                else: st.dataframe(df, use_container_width=True)
    conn.close()

elif choice == "Manage Database (CRUD)":
    st.header("View & Manage Database Tables")
    if st.session_state.action_msg:
        st.success(st.session_state.action_msg)
        st.session_state.action_msg = None 
        
    try:
        conn = get_connection()
        tables = ["JobPosting", "Skill", "JobSkill", "Candidate", "Resume", "CandidateSkill", "ScreeningResult", "ScreeningDetail"]
        selected_table = st.selectbox("Select Table to Manage", tables)
        
        df = fetch_df(conn, f"SELECT * FROM {selected_table}")
        cols = list(df.columns)
        
        if not df.empty:
            pk_col = cols[0] 
            st.dataframe(df, use_container_width=True)
            st.divider()
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader(f"✏️ Update {selected_table}")
                edit_id = st.selectbox(f"Select {pk_col} to Edit", df[pk_col].tolist())
                current_row = df[df[pk_col] == edit_id].iloc[0]
                
                with st.form("edit_form"):
                    new_values = {col: st.text_input(col, value=str(current_row[col]), disabled=(col == pk_col)) for col in cols}
                    if st.form_submit_button("Save Changes"):
                        try:
                            set_clause = ", ".join([f"{col} = %s" for col in cols if col != pk_col])
                            params = [None if p in ["None", ""] else p for k, p in new_values.items() if k != pk_col]
                            params.append(edit_id)
                            conn.cursor().execute(f"UPDATE {selected_table} SET {set_clause} WHERE {pk_col} = %s", tuple(params))
                            conn.commit()
                            st.session_state.action_msg = f"✅ Updated record '{edit_id}'!"
                            st.rerun()
                        except mysql.connector.Error as e: st.error(f"Database Error: {e}")
            with col2:
                st.subheader(f"🗑️ Delete {selected_table}")
                del_id = st.selectbox(f"Select {pk_col} to Delete", df[pk_col].tolist(), key="del_box")
                if st.button("Delete Record", type="primary"):
                    try:
                        conn.cursor().execute(f"DELETE FROM {selected_table} WHERE {pk_col} = %s", (del_id,))
                        conn.commit()
                        st.session_state.action_msg = f"🗑️ Deleted record '{del_id}'!"
                        st.rerun()
                    except mysql.connector.Error as e: st.error(f"Database Error: {e}")
        else:
            st.dataframe(df, use_container_width=True) 
            st.info("Table is empty.")
        conn.close()
    except Exception as e: st.error(f"Error: {e}")