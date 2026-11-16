# 🧠 AI Resume Screening System

An end-to-end resume screening platform that parses PDF resumes, scores candidates against job postings using NLP embeddings, and stores everything in a normalized MySQL database. It also demonstrates core DBMS concepts: triggers, stored procedures, normalization (1NF–5NF), ACID transactions, and concurrency control.

Built with **Python, Streamlit, MySQL, and Sentence-Transformers**.

---

## ✨ Features

- **Resume parsing**: extracts text from PDFs (PyPDF2) and pulls out name, email, phone, years of experience, and skills.
- **AI match scoring**: weighted score combining skill match, proficiency, experience, and semantic similarity (`all-MiniLM-L6-v2`).
- **Database-driven decisions**: a MySQL `BEFORE INSERT` trigger sets the status (Shortlisted / Pending / Rejected) from the score.
- **Stored procedure reports**: `sp_GetTopCandidates` ranks candidates per job.
- **Atomic transactions**: one upload writes to 5 tables, with full rollback on any failure.
- **Concurrency control demo**: simulates two recruiters using `SELECT ... FOR UPDATE`, row-level X-locks, and the effect of `COMMIT` vs `ROLLBACK`.
- **Normalization explorer**: interactive 1NF–5NF explanation for every table, with live schema and sample data.
- **CRUD admin panel**: view, edit, and delete records in any table.

## 🧮 Scoring Formula

| Component | Weight |
|---|---|
| Skill match | 35% |
| Proficiency | 15% |
| Experience match | 25% |
| Semantic similarity (cosine) | 25% |

| Score | Status (set by MySQL trigger) |
|---|---|
| ≥ 80 | Shortlisted |
| 60 – 79 | Pending |
| < 60 | Rejected |

## 🗂️ Database Schema

`Candidate`, `Resume`, `Skill`, `CandidateSkill`, `JobPosting`, `JobSkill`, `ScreeningResult`, `ScreeningDetail`

<!-- Add your ER diagram here: ![ER Diagram](docs/er_diagram.png) -->

## 🛠️ Tech Stack

- **Frontend:** Streamlit
- **Backend:** Python 3.10+
- **Database:** MySQL 8 (InnoDB)
- **NLP:** sentence-transformers, PyPDF2
- **Data:** pandas

## 🚀 Getting Started

### 1. Clone the repository
```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
```

### 2. Create a virtual environment
```bash
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS / Linux
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure environment variables
Copy `.env.example` to `.env` and fill in your MySQL credentials:
```env
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your_password_here
DB_NAME=dbmsproject
```

### 5. Run the app
```bash
streamlit run streamlit_app.py
```
Tables, triggers, the stored procedure, and sample data are created automatically on first run.

## 📁 Project Structure

```
├── streamlit_app.py     # Main application
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
├── .gitignore
└── README.md
```

## 📸 Screenshots

<!-- Add screenshots to a /docs folder and link them, e.g. -->
<!-- ![Upload page](docs/upload.png) -->
<!-- ![Concurrency demo](docs/concurrency.png) -->

## 🔮 Future Improvements

- Support DOCX resumes
- Smarter NER-based name and skill extraction
- User authentication and recruiter roles
- Docker setup for one-command deployment

## 📄 License

MIT. See [LICENSE](LICENSE).

## 👤 Author

**Your Name**: [GitHub](https://github.com/your-username) · [LinkedIn](https://linkedin.com/in/your-profile)
